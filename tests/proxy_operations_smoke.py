"""Dedicated shared-proxy test on ephemeral LOOPBACK ports, synthetic hostname/TLS.

Refuses to run if any existing booking-proxy container/network exists. No public ACME
or Telegram calls. Tests real Docker API restrictions, routing and proxy outages.
"""

import argparse
import ipaddress
import json
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

from booking_bot.deployment import proxy
from booking_bot.deployment.files import DeploymentError, atomic_write, private_directory
from booking_bot.deployment.manager import DeploymentManager, run_docker
from booking_bot.deployment.monitoring import host_report

SERVER = (
    "from http.server import HTTPServer,BaseHTTPRequestHandler; "
    "Handler=type('Handler',(BaseHTTPRequestHandler,),"
    "{'do_GET':lambda self:(self.send_response(200),self.end_headers(),"
    "self.wfile.write(b'phase7-ok'))});HTTPServer(('0.0.0.0',8000),Handler).serve_forever()"
)
ACL_PROBE = """
import urllib.request, urllib.error
def status(path, method='GET'):
    request = urllib.request.Request('http://socket-proxy:2375' + path, method=method)
    try:
        with urllib.request.urlopen(request,timeout=5) as response: return response.status
    except urllib.error.HTTPError as error: return error.code
assert status('/version') == 200
assert status('/images/json') == 403
assert status('/containers/phase7-nonexistent/stop','POST') == 403
print('DOCKER API ACL PASSED: GET version allowed, images and POST denied')
"""


def smoke(root: Path, image: str, network_pool: str, *, resume=False, socket_image=None):
    pool = ipaddress.ip_network(network_pool)
    if pool.version != 4 or pool.prefixlen != 23 or not pool.is_private:
        raise ValueError("Proxy test pool must be a private IPv4 /23")
    subnets = list(pool.subnets(new_prefix=24))
    if root.exists() and not resume:
        raise RuntimeError("Use a NEW private proxy test registry")
    existing = run_docker(
        ["ps", "-aq", "--filter", "label=com.docker.compose.project=booking-proxy"]
    ).split()
    directory = proxy.proxy_directory(root)
    if resume:
        if not root.exists() or not existing:
            raise RuntimeError("No owned proxy test to resume")
        containers = json.loads(run_docker(["inspect", *existing]))
        if any(
            Path(c["Config"]["Labels"]["com.docker.compose.project.working_dir"]).resolve()
            != directory.resolve()
            for c in containers
        ):
            raise RuntimeError("Proxy containers do not belong to this test directory")
    elif existing:
        raise RuntimeError("Existing booking-proxy project: test refuses to modify it")
    if (
        not resume
        and proxy.PROXY_NETWORK
        in run_docker(["network", "ls", "--format", "{{.Name}}"]).splitlines()
    ):
        raise RuntimeError("Existing proxy network: test refuses to modify it")
    private_directory(root)
    private_directory(root.parent / "backups")
    if not resume:
        proxy.initialize(
            root,
            email="ops-test@example.org",
            server_ipv4="127.0.0.1",
            server_ipv6=None,
            staging=False,
        )
    model = proxy.proxy_compose(False)
    if socket_image:
        model["services"]["socket-proxy"]["image"] = socket_image
        model["services"]["socket-proxy"]["pull_policy"] = "never"
    model["services"]["traefik"]["ports"] = [
        {"target": p, "published": "0", "host_ip": "127.0.0.1"} for p in (80, 443)
    ]
    model["services"]["traefik"]["command"].append(
        "--providers.docker.constraints=Label(`com.docker.compose.project`,`booking-proxy`)"
    )
    model["networks"]["docker-api"]["ipam"] = {"config": [{"subnet": str(subnets[1])}]}
    model["services"]["probe"] = {
        "image": image,
        "command": ["python", "-c", SERVER],
        "networks": ["proxy"],
        "labels": {
            "traefik.enable": "true",
            "traefik.docker.network": "booking-proxy",
            "traefik.http.routers.phase7.rule": "Host(`ops-test.invalid`)",
            "traefik.http.routers.phase7.entrypoints": "websecure",
            "traefik.http.routers.phase7.tls": "true",
            "traefik.http.services.phase7.loadbalancer.server.port": "8000",
        },
    }
    atomic_write(directory / "compose.yaml", json.dumps(model))
    if not resume:
        run_docker(["network", "create", "--subnet", str(subnets[0]), proxy.PROXY_NETWORK])
    proxy.compose(root, "up", "-d", "--wait")
    ids = proxy.compose(root, "ps", "--all", "-q").split()
    traefik = proxy.compose(root, "ps", "-q", "traefik").strip()

    # Routing test uses Traefik's default self-signed cert. No claim of public TLS validity.
    def routed():
        try:
            port = json.loads(run_docker(["inspect", traefik]))[0]["NetworkSettings"]["Ports"][
                "443/tcp"
            ][0]["HostPort"]
            request = urllib.request.Request(
                f"https://127.0.0.1:{port}/live", headers={"Host": "ops-test.invalid"}
            )
            with urllib.request.urlopen(
                request, context=ssl._create_unverified_context(), timeout=5
            ) as response:
                return response.status == 200 and response.read() == b"phase7-ok"
        except (OSError, DeploymentError, KeyError, TypeError, urllib.error.HTTPError):
            return False

    deadline = time.monotonic() + 45
    while not routed():
        if time.monotonic() > deadline:
            raise AssertionError("Proxy did not route via restricted Docker API")
        time.sleep(2)
    output = run_docker(
        [
            "run",
            "--rm",
            "--network",
            "booking-proxy_docker-api",
            "--entrypoint",
            "python",
            image,
            "-c",
            ACL_PROBE,
        ]
    )
    print(output.strip(), flush=True)
    proxy.compose(
        root,
        "exec",
        "-T",
        "probe",
        "python",
        "-c",
        "import socket; assert socket.getaddrinfo('probe',8000); "
        "\ntry: socket.getaddrinfo('socket-proxy',2375)\nexcept OSError: pass\n"
        "else: raise AssertionError('Client can access Docker API network')",
    )
    manager = DeploymentManager(root)
    healthy = host_report(manager)
    assert next(c for c in healthy["checks"] if c["check"] == "proxy")["severity"] == "OK"
    proxy.compose(root, "stop", "traefik")
    assert not routed()
    # Backend remains alive while the shared proxy is stopped.
    proxy.compose(
        root,
        "exec",
        "-T",
        "probe",
        "python",
        "-c",
        "import urllib.request; assert urllib.request.urlopen('http://localhost:8000/live').status==200",
    )
    failed = host_report(manager)
    assert next(c for c in failed["checks"] if c["check"] == "proxy")["severity"] == "CRITICAL"
    proxy.compose(root, "up", "-d", "--wait", "traefik")
    deadline = time.monotonic() + 45
    while not routed():
        if time.monotonic() > deadline:
            raise AssertionError("Routing did not recover")
        time.sleep(2)
    proxy.compose(root, "restart", "traefik")
    deadline = time.monotonic() + 45
    while not routed():
        if time.monotonic() > deadline:
            raise AssertionError("Routing did not recover after restart")
        time.sleep(2)
    print(
        "PROXY ACCEPTANCE PASSED: routing, ACL, isolation, outage, backend alive, restart",
        flush=True,
    )
    atomic_write(
        root.parent / "proxy-results.json",
        json.dumps(
            {
                "routing": "passed with default self-signed certificate",
                "docker_api_acl": "passed",
                "outage_restart": "passed",
                "public_dns_trusted_tls": "not tested",
            }
        ),
    )
    # Remove exact test containers only; keep ACME volume and private artifacts.
    run_docker(["rm", "-f", *ids])
    run_docker(["network", "rm", "booking-proxy_docker-api", proxy.PROXY_NETWORK])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--network-pool", default="10.252.28.0/23")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--socket-image", help="Built/scanned candidate; no registry publication needed"
    )
    args = parser.parse_args()
    smoke(
        args.root.absolute(),
        args.image,
        args.network_pool,
        resume=args.resume,
        socket_image=args.socket_image,
    )

"""Opt-in local two-client Traefik smoke without DNS, ACME or real Telegram tokens.

Run after smoke_deployments.py against its dedicated registry. Client data remains;
API Compose files and containers return to their original private configuration.
"""

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from booking_bot.deployment.files import DeploymentError, atomic_write
from booking_bot.deployment.manager import DeploymentManager, run_docker
from booking_bot.deployment.templates import compose_model


def request(host: str, port: int, *, secret: str | None = None, update_id: int = 0) -> int:
    url = f"http://127.0.0.1:{port}"
    if secret is None:
        path, data, headers = "/ready", None, {"Host": host}
    else:
        path = "/api/v1/webhooks/telegram"
        data = json.dumps({"update_id": update_id}).encode()
        headers = {
            "Host": host,
            "Content-Type": "application/json",
            "X-Telegram-Bot-Api-Secret-Token": secret,
        }
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url + path, data=data, headers=headers), timeout=5
        ) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def smoke(root: Path, port: int, subnet: str | None) -> None:
    manager = DeploymentManager(root)
    clients = ("smoke-alice", "smoke-bob")
    original = {}
    for slug in clients:
        if manager.state(slug)["status"] != "READY":
            raise RuntimeError(f"{slug} must be READY")
        path = manager.directory(slug) / "compose.yaml"
        original[slug] = path.read_text(encoding="utf-8")
    try:
        run_docker(["network", "inspect", "booking-proxy"])
    except DeploymentError:
        pass
    else:
        raise RuntimeError("booking-proxy already exists; refusing to touch production network")
    network_args = ["network", "create", *(["--subnet", subnet] if subnet else []), "booking-proxy"]
    run_docker(network_args)
    started_proxy = False
    try:
        run_docker(
            [
                "run",
                "-d",
                "--rm",
                "--name",
                "booking-phase4-smoke-proxy",
                "--network",
                "booking-proxy",
                "-p",
                f"127.0.0.1:{port}:80",
                "-v",
                "/var/run/docker.sock:/var/run/docker.sock:ro",
                "traefik:v3.7.13",
                "--entrypoints.web.address=:80",
                "--providers.docker=true",
                "--providers.docker.exposedbydefault=false",
                "--providers.docker.network=booking-proxy",
            ]
        )
        started_proxy = True
        for slug in clients:
            state = manager.state(slug)
            domain = f"{slug}.test"
            model = compose_model(state["project"], domain, public=True)
            model["networks"]["egress"] = json.loads(original[slug])["networks"]["egress"]
            labels = model["services"]["api"]["labels"]
            for key in list(labels):
                if key.endswith(".tls.certresolver"):
                    del labels[key]
                elif key.endswith(".entrypoints"):
                    labels[key] = "web"
            atomic_write(manager.directory(slug) / "compose.yaml", json.dumps(model, indent=2))
            manager.compose(slug, "up", "-d", "--wait", "--no-deps", "--force-recreate", "api")
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                if all(request(f"{slug}.test", port) == 200 for slug in clients):
                    break
            except OSError:
                pass
            time.sleep(2)
        else:
            raise AssertionError("Both Host routes did not become ready")
        first, second = (manager.values(slug)["TELEGRAM_WEBHOOK_HEADER_SECRET"] for slug in clients)
        assert first != second
        assert request("smoke-alice.test", port, secret=first, update_id=900001) == 200
        assert request("smoke-bob.test", port, secret=second, update_id=900001) == 200
        assert request("smoke-alice.test", port, secret=second, update_id=900002) == 403
        assert request("smoke-bob.test", port, secret=first, update_id=900002) == 403
        for slug in clients:
            rows = manager.compose(
                slug,
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "booking",
                "-d",
                "booking",
                "-Atqc",
                "SELECT count(*) FROM telegram_update_receipts WHERE update_id=900001",
            )
            assert rows.strip() == "1", (slug, rows)
        run_docker(["restart", "booking-phase4-smoke-proxy"])
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                if all(request(f"{slug}.test", port) == 200 for slug in clients):
                    break
            except OSError:
                pass
            time.sleep(2)
        else:
            raise AssertionError("Host routes did not recover after Traefik restart")
        print("Two routes, webhook isolation, separate DB receipts, proxy restart: OK")
    finally:
        for slug in clients:
            atomic_write(manager.directory(slug) / "compose.yaml", original[slug])
            manager.compose(slug, "up", "-d", "--wait", "--no-deps", "--force-recreate", "api")
        if started_proxy:
            run_docker(["stop", "booking-phase4-smoke-proxy"])
        run_docker(["network", "rm", "booking-proxy"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--subnet", help="Optional free local subnet if Docker pools are exhausted")
    args = parser.parse_args()
    smoke(args.root, args.port, args.subnet)

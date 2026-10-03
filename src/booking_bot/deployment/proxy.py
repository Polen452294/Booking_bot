"""Shared Traefik deployment and public endpoint preflight."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

from booking_bot.deployment.files import DeploymentError, atomic_write, private_directory, read_json
from booking_bot.deployment.manager import run_docker, validate_domain
from booking_bot.deployment.templates import LOGGING
from booking_bot.version import SOCKET_IMAGE_REPOSITORY, __version__

PROXY_NETWORK = "booking-proxy"
TRAEFIK_VERSION = "v3.7.13"


def webhook_url(domain: str) -> str:
    return f"https://{validate_domain(domain)}/api/v1/webhooks/telegram"


def proxy_directory(root: Path) -> Path:
    return root.parent / "infra" / "proxy"


def proxy_compose(staging: bool) -> dict:
    filename = "acme-staging.json" if staging else "acme-production.json"
    command = [
        "--entrypoints.web.address=:80",
        "--entrypoints.websecure.address=:443",
        "--entrypoints.web.http.redirections.entrypoint.to=websecure",
        "--entrypoints.web.http.redirections.entrypoint.scheme=https",
        "--providers.docker=true",
        "--providers.docker.endpoint=tcp://socket-proxy:2375",
        "--providers.docker.exposedbydefault=false",
        f"--providers.docker.network={PROXY_NETWORK}",
        "--certificatesresolvers.letsencrypt.acme.email=${ACME_EMAIL}",
        f"--certificatesresolvers.letsencrypt.acme.storage=/letsencrypt/{filename}",
        "--certificatesresolvers.letsencrypt.acme.httpchallenge.entrypoint=web",
    ]
    if staging:
        command.append(
            "--certificatesresolvers.letsencrypt.acme.caserver="
            "https://acme-staging-v02.api.letsencrypt.org/directory"
        )
    return {
        "name": "booking-proxy",
        "services": {
            "socket-proxy": {
                "image": f"{SOCKET_IMAGE_REPOSITORY}:{__version__}",
                "environment": {
                    "CONTAINERS": "1",
                    "NETWORKS": "1",
                    "EVENTS": "1",
                    "PING": "1",
                    "VERSION": "1",
                    "POST": "0",
                    "INFO": "0",
                    "IMAGES": "0",
                    "VOLUMES": "0",
                    "AUTH": "0",
                    "EXEC": "0",
                    "SECRETS": "0",
                    "BUILD": "0",
                    "ALLOW_START": "0",
                    "ALLOW_STOP": "0",
                    "ALLOW_RESTARTS": "0",
                    "ALLOW_PAUSE": "0",
                    "ALLOW_UNPAUSE": "0",
                },
                "volumes": ["/var/run/docker.sock:/var/run/docker.sock:ro"],
                "networks": ["docker-api"],
                "restart": "unless-stopped",
                "logging": LOGGING,
                "security_opt": ["no-new-privileges:true"],
            },
            "init-acme": {
                "logging": LOGGING,
                "image": f"traefik:{TRAEFIK_VERSION}",
                "entrypoint": ["/bin/sh", "-c"],
                "command": [
                    "umask 077; touch /letsencrypt/acme-staging.json "
                    "/letsencrypt/acme-production.json; "
                    "chmod 600 /letsencrypt/acme-staging.json "
                    "/letsencrypt/acme-production.json"
                ],
                "volumes": ["letsencrypt:/letsencrypt"],
                "network_mode": "none",
                "restart": "no",
            },
            "traefik": {
                "logging": LOGGING,
                "image": f"traefik:{TRAEFIK_VERSION}",
                "command": command,
                "depends_on": {
                    "init-acme": {"condition": "service_completed_successfully"},
                    "socket-proxy": {"condition": "service_started"},
                },
                "ports": ["80:80", "443:443"],
                "volumes": [
                    "letsencrypt:/letsencrypt",
                ],
                "networks": ["proxy", "docker-api"],
                "restart": "unless-stopped",
                "security_opt": ["no-new-privileges:true"],
            },
        },
        "networks": {
            "proxy": {"external": True, "name": PROXY_NETWORK},
            "docker-api": {"internal": True},
        },
        "volumes": {"letsencrypt": {}},
    }


def config(root: Path) -> dict:
    path = proxy_directory(root)
    if not (path / "settings.json").exists():
        raise DeploymentError("Proxy is not initialized; use bookingctl proxy init")
    result = read_json(path / "settings.json")
    if (
        type(result.get("staging")) is not bool
        or not isinstance(result.get("email"), str)
        or not isinstance(result.get("server_ipv4"), str)
        or result.get("server_ipv6") is not None
        and not isinstance(result.get("server_ipv6"), str)
    ):
        raise DeploymentError("Invalid proxy configuration")
    try:
        if result["server_ipv4"] and ipaddress.ip_address(result["server_ipv4"]).version != 4:
            raise ValueError
        if result["server_ipv6"] and ipaddress.ip_address(result["server_ipv6"]).version != 6:
            raise ValueError
    except ValueError:
        raise DeploymentError("Invalid proxy IP configuration") from None
    return result


def set_mode(root: Path, *, staging: bool) -> None:
    path = proxy_directory(root)
    settings = config(root)
    settings["staging"] = staging
    atomic_write(path / "compose.yaml", json.dumps(proxy_compose(staging), indent=2) + "\n")
    atomic_write(path / "settings.json", json.dumps(settings, indent=2) + "\n")


def initialize(
    root: Path, *, email: str, server_ipv4: str, server_ipv6: str | None, staging: bool
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", email):
        raise DeploymentError("Provide a valid ACME email")
    for value, version in ((server_ipv4, 4), (server_ipv6, 6)):
        try:
            if value and ipaddress.ip_address(value).version != version:
                raise ValueError
        except ValueError:
            raise DeploymentError(f"Expected an IPv{version} server address") from None
    if not server_ipv4 and not server_ipv6:
        raise DeploymentError("Provide the VPS public IPv4 or IPv6 address")
    path = proxy_directory(root)
    if (path / "settings.json").exists():
        raise DeploymentError("Proxy already initialized; preserve ACME state and configuration")
    private_directory(path)
    atomic_write(path / ".env", f"ACME_EMAIL={email}\n")
    atomic_write(path / "compose.yaml", json.dumps(proxy_compose(staging), indent=2) + "\n")
    atomic_write(
        path / "settings.json",
        json.dumps(
            {
                "email": email,
                "server_ipv4": server_ipv4,
                "server_ipv6": server_ipv6,
                "staging": staging,
            },
            indent=2,
        )
        + "\n",
    )


def compose(root: Path, *args: str) -> str:
    path = proxy_directory(root)
    settings = config(root)
    return run_docker(
        [
            "compose",
            "--project-name",
            "booking-proxy",
            "--project-directory",
            str(path),
            "--env-file",
            str(path / ".env"),
            "-f",
            str(path / "compose.yaml"),
            *args,
        ],
        values={"ACME_EMAIL": settings["email"]},
    )


def ensure_network() -> None:
    try:
        details = json.loads(run_docker(["network", "inspect", PROXY_NETWORK]))[0]
    except DeploymentError:
        run_docker(["network", "create", PROXY_NETWORK])
        details = json.loads(run_docker(["network", "inspect", PROXY_NETWORK]))[0]
    if details.get("Driver") != "bridge" or details.get("Internal"):
        raise DeploymentError("booking-proxy must be an external bridge network")


def status(root: Path) -> bool:
    config(root)
    try:
        details = json.loads(run_docker(["network", "inspect", PROXY_NETWORK]))[0]
        if details.get("Driver") != "bridge" or details.get("Internal"):
            return False
        rows = compose(root, "ps", "--format", "json")
        entries = (
            json.loads(rows)
            if rows.lstrip().startswith("[")
            else [json.loads(line) for line in rows.splitlines() if line.strip()]
        )
        return any(
            row.get("Service") == "traefik" and row.get("State") == "running" for row in entries
        )
    except (DeploymentError, ValueError, IndexError, KeyError):
        return False


def domain_check(root: Path, domain: str) -> dict:
    hostname = validate_domain(domain)
    if not hostname:
        raise DeploymentError("Domain is not configured; use expose SLUG --domain HOSTNAME")
    settings = config(root)
    try:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        }
    except OSError:
        addresses = set()
    expected = {
        ipaddress.ip_address(value)
        for value in (settings.get("server_ipv4"), settings.get("server_ipv6"))
        if value
    }
    ok = bool(addresses) and addresses <= expected
    return {
        "domain": hostname,
        "resolved": sorted(str(ip) for ip in addresses),
        "expected": sorted(str(ip) for ip in expected),
        "ok": ok,
    }


def https_check(domain: str, *, timeout_seconds: int = 120, staging: bool = False) -> dict:
    origin = f"https://{validate_domain(domain)}"
    deadline = time.monotonic() + timeout_seconds
    result = {"live": False, "ready": False}
    context = ssl._create_unverified_context() if staging else ssl.create_default_context()
    while time.monotonic() < deadline:
        for name in result:
            try:
                with urllib.request.urlopen(
                    origin + "/" + name, timeout=5, context=context
                ) as response:
                    result[name] = response.status == 200
            except (OSError, urllib.error.HTTPError):
                result[name] = False
        if all(result.values()):
            return result
        time.sleep(3)
    return result

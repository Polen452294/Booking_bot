import ipaddress
import socket

import pytest

from booking_bot.deployment import proxy
from booking_bot.deployment.files import DeploymentError
from booking_bot.deployment.manager import validate_domain


@pytest.mark.parametrize(
    "hostname",
    [
        "https://example.org",
        "example.org/path",
        "example.org?x=1",
        "example.org:443",
        "192.0.2.1",
        "EXAMPLE.ORG",
        "example.org\nHost(`evil.org`)",
    ],
)
def test_domain_rejects_non_hostnames(hostname):
    with pytest.raises(DeploymentError):
        validate_domain(hostname)


def test_proxy_compose_staging_has_separate_acme_storage():
    production = proxy.proxy_compose(False)
    staging = proxy.proxy_compose(True)
    assert production["services"]["traefik"]["image"] == "traefik:v3.7.13"
    assert set(production["services"]["traefik"]["ports"]) == {"80:80", "443:443"}
    assert not any("api.insecure" in arg for arg in production["services"]["traefik"]["command"])
    prod_flags = " ".join(production["services"]["traefik"]["command"])
    stage_flags = " ".join(staging["services"]["traefik"]["command"])
    assert "exposedbydefault=false" in prod_flags
    assert "acme-production.json" in prod_flags
    assert "acme-staging.json" in stage_flags
    assert "acme-staging-v02" in stage_flags


def test_proxy_initialize_and_dns_preflight(tmp_path, monkeypatch):
    root = tmp_path / "clients"
    proxy.initialize(
        root, email="ops@example.org", server_ipv4="203.0.113.8", server_ipv6=None, staging=True
    )
    path = proxy.proxy_directory(root)
    assert path == tmp_path / "infra" / "proxy"
    assert proxy.proxy_compose(True)["volumes"]["letsencrypt"] == {}
    assert "chmod 600" in proxy.proxy_compose(True)["services"]["init-acme"]["command"][0]
    assert proxy.config(root)["staging"] is True

    def resolve(_hostname, _port, *, type):
        assert type == socket.SOCK_STREAM
        return [(socket.AF_INET, 0, 0, "", ("203.0.113.8", 443))]

    monkeypatch.setattr(proxy.socket, "getaddrinfo", resolve)
    assert proxy.domain_check(root, "alice.example.org")["ok"]
    monkeypatch.setattr(
        proxy.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, 0, 0, "", ("203.0.113.9", 443))],
    )
    assert not proxy.domain_check(root, "alice.example.org")["ok"]
    assert ipaddress.ip_address(proxy.config(root)["server_ipv4"]).version == 4

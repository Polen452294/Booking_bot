import io
import json
import runpy
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from booking_bot.version import __version__

guard = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/verify_release_image.py")
)["verify_unused"]


@pytest.fixture
def package_api(monkeypatch):
    monkeypatch.setattr(
        guard.__globals__["subprocess"],
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stderr="denied"),
    )
    responses = []

    def response(request, **kw):
        assert request.get_header("Authorization") == "Bearer synthetic-token"
        page = responses.pop(0)
        if isinstance(page, int):
            raise HTTPError(request.full_url, page, "test", {}, None)
        return io.BytesIO(json.dumps(page).encode())

    monkeypatch.setattr(guard.__globals__["urllib"].request, "urlopen", response)
    return responses


def test_initial_package_404_is_allowed(package_api):
    package_api.append(404)
    guard("synthetic-token")


@pytest.mark.parametrize("package", ["booking-socket-proxy", "booking-postgres", "booking-redis"])
def test_infrastructure_package_is_guarded_independently(monkeypatch, package):
    calls = []

    def manifest(command, **kwargs):
        calls.append(command[-1])
        return SimpleNamespace(returncode=1, stderr="denied")

    def response(request, **kwargs):
        calls.append(request.full_url)
        return io.BytesIO(b"[]")

    monkeypatch.setattr(guard.__globals__["subprocess"], "run", manifest)
    monkeypatch.setattr(guard.__globals__["urllib"].request, "urlopen", response)
    guard("synthetic-token", repository=f"ghcr.io/polen452294/{package}")
    assert calls[0] == f"ghcr.io/polen452294/{package}:{__version__}"
    assert f"/packages/container/{package}/versions" in calls[1]


@pytest.mark.parametrize("code", [401, 403, 500])
def test_unknown_permissions_or_server_error_refuses_publish(package_api, code):
    package_api.append(code)
    with pytest.raises(ValueError, match="publication refused"):
        guard("synthetic-token")


def test_full_release_tag_on_later_page_is_protected(package_api):
    package_api.extend(
        [
            [{"metadata": {"container": {"tags": ["old"]}}}] * 100,
            [{"metadata": {"container": {"tags": [__version__]}}}],
        ]
    )
    with pytest.raises(ValueError, match="already exists"):
        guard("synthetic-token")


def test_manifest_success_always_refuses_reuse(monkeypatch):
    monkeypatch.setattr(
        guard.__globals__["subprocess"],
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stderr=""),
    )
    with pytest.raises(ValueError, match="already exists"):
        guard("synthetic-token")


def test_registry_transport_error_is_not_treated_as_missing(monkeypatch):
    monkeypatch.setattr(
        guard.__globals__["subprocess"],
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stderr="timeout"),
    )
    with pytest.raises(ValueError, match="availability unknown"):
        guard("synthetic-token")

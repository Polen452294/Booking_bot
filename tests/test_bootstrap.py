"""Bootstrap repeat/recovery boundaries; all host commands are intercepted."""

import json
import runpy
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture
def installer(tmp_path, monkeypatch):
    namespace = runpy.run_path(str(SOURCE / "scripts/bootstrap_server.py"))
    bootstrap = namespace["bootstrap"]
    root = tmp_path / "booking"
    launcher = tmp_path / "bookingctl"
    calls = []

    def run(*args):
        calls.append(tuple(str(a) for a in args))
        if "init" in args:
            directory = root / "infra/proxy"
            directory.mkdir()
            (directory / "settings.json").write_text(
                json.dumps(
                    {
                        "email": "rc@example.org",
                        "server_ipv4": "192.0.2.1",
                        "server_ipv6": None,
                        "staging": False,
                    }
                )
            )

    for key, value in (("ROOT", root), ("WRAPPER", launcher), ("run", run)):
        monkeypatch.setitem(bootstrap.__globals__, key, value)
    return bootstrap, root, launcher, calls


def test_repeat_preserves_clients_secrets_backups_and_proxy(installer):
    bootstrap, root, _, calls = installer
    bootstrap(SOURCE, email="rc@example.org", ipv4="192.0.2.1")
    for slug in ("release-test-1", "release-test-2"):
        directory = root / "clients" / slug
        directory.mkdir()
        (directory / ".env").write_text("test secrets must remain byte-identical")
    (root / "backups/test.dump").write_bytes(b"synthetic dump")
    template = root / "infra/specialist.toml"
    template.write_text("operator edited template")
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    calls.clear()
    bootstrap(SOURCE)
    assert before == {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert not any("pip" in call or "init" in call or "venv" in call for call in calls)
    assert any("doctor" in call for call in calls)
    assert not any("down" in call or "volume" in call for call in calls)


def test_prepare_only_then_full_install_reuses_cli(installer):
    bootstrap, _, _, calls = installer
    bootstrap(SOURCE, prepare_only=True)
    assert not any("proxy" in call or "schedule" in call for call in calls)
    calls.clear()
    bootstrap(SOURCE, email="rc@example.org", ipv4="192.0.2.1")
    assert not any("pip" in call for call in calls)
    assert any("init" in call for call in calls)
    assert sum("install" in call for call in calls) == 2  # existing two timers


def test_failed_runtime_can_resume_without_reinstall_or_reset(installer, monkeypatch):
    bootstrap, root, _, calls = installer
    run = bootstrap.__globals__["run"]

    def fail(*args):
        if args[-2:] == ("proxy", "start"):
            raise subprocess.CalledProcessError(1, ["proxy", "start"])
        run(*args)

    monkeypatch.setitem(bootstrap.__globals__, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        bootstrap(SOURCE, email="rc@example.org", ipv4="192.0.2.1")
    settings = (root / "infra/proxy/settings.json").read_bytes()
    calls.clear()
    monkeypatch.setitem(bootstrap.__globals__, "run", run)
    bootstrap(SOURCE)
    assert (root / "infra/proxy/settings.json").read_bytes() == settings
    assert not any("init" in call or "pip" in call for call in calls)


def test_proxy_settings_cannot_change_implicitly(installer):
    bootstrap, root, _, _ = installer
    bootstrap(SOURCE, email="rc@example.org", ipv4="192.0.2.1")
    settings = (root / "infra/proxy/settings.json").read_bytes()
    with pytest.raises(ValueError, match="settings differ"):
        bootstrap(SOURCE, ipv4="192.0.2.2")
    assert (root / "infra/proxy/settings.json").read_bytes() == settings


def test_cli_version_change_requires_explicit_upgrade(installer):
    bootstrap, root, _, calls = installer
    bootstrap(SOURCE, prepare_only=True)
    (root / "state/bootstrap.json").write_text('{"version":"0.7.0"}')
    calls.clear()
    with pytest.raises(ValueError, match="another version"):
        bootstrap(SOURCE, prepare_only=True)
    assert not calls


def test_existing_foreign_launcher_is_preserved(installer):
    bootstrap, _, launcher, _ = installer
    launcher.write_text("unrelated operator launcher")
    with pytest.raises(ValueError, match="launcher differs"):
        bootstrap(SOURCE, prepare_only=True)
    assert launcher.read_text() == "unrelated operator launcher"

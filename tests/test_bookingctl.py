import io
import json
import os
import subprocess
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest

from booking_bot.deployment import manager as module
from booking_bot.deployment import proxy
from booking_bot.deployment.files import DeploymentError, registry_lock
from booking_bot.deployment.manager import (
    BotIdentity,
    CreateRequest,
    DeploymentManager,
    docker_environment,
    get_bot_identity,
    run_docker,
    validate_domain,
    validate_image,
    validate_slug,
)
from booking_bot.specialist_config import load_specialist_template

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "123456789:" + "aB9_Yz7-" * 5
IMAGE = "booking-bot:0.3.0-test"


def request(slug="alice", token=TOKEN):
    template = load_specialist_template(ROOT / "specialist.toml")
    template = replace(template, profile=replace(template.profile, slug=slug))
    return CreateRequest(template, token, IMAGE)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "docker_preflight", lambda: None)
    monkeypatch.setattr(module, "resolve_image", lambda ref: "sha256:" + "a" * 64)
    monkeypatch.setattr(module, "run_docker", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        module,
        "get_bot_identity",
        lambda token: BotIdentity(int(token.split(":")[0]), "test_booking_bot"),
    )
    instance = DeploymentManager(tmp_path / "registry")
    calls = []

    def compose(slug, *args, **kwargs):
        calls.append(args)
        return "Invite link: https://t.me/test_booking_bot?start=master_fakeinvite\n"

    monkeypatch.setattr(instance, "compose", compose)
    instance.calls = calls
    return instance


@pytest.mark.parametrize(
    "slug",
    [
        "../alice",
        "Alice",
        "a_b",
        "a--b",
        "a-",
        "-a",
        "",
        "a" * 41,
        "con",
        "nul",
        "com1",
        "аnna",
        "a/b",
    ],
)
def test_invalid_slug(slug):
    with pytest.raises(DeploymentError):
        validate_slug(slug)


@pytest.mark.parametrize(
    "image", ["booking-bot", "booking-bot:latest", "x:stable", "-bad:v1", "x:v1\nBAD=1", "x:master"]
)
def test_unversioned_images_rejected(image):
    with pytest.raises(DeploymentError):
        validate_image(image)


def test_ghcr_digest():
    value = "ghcr.io/polen452294/booking_bot@sha256:" + "a" * 64
    assert validate_image(value) == value


@pytest.mark.parametrize("domain", ["https://x.org", "x.org/path", "x.org:443", "x.org\nENV=1"])
def test_domain_injection_rejected(domain):
    with pytest.raises(DeploymentError):
        validate_domain(domain)


def test_duplicate_domain_rejected(manager):
    first = request()
    first.domain = "anna.example.org"
    manager.create("alice", first)
    second = request("bob", TOKEN.replace("123456789", "123456780"))
    second.domain = "anna.example.org"
    with pytest.raises(DeploymentError, match="Domain already"):
        manager.create("bob", second)
    assert not manager.directory("bob").exists()


def test_public_compose_only_connects_api_to_proxy():
    from booking_bot.deployment.templates import compose_model

    private = compose_model("booking-alice-12345678")
    assert "proxy" not in private["networks"]
    assert private["services"]["api"]["ports"][0]["host_ip"] == "127.0.0.1"
    public = compose_model("booking-alice-12345678", "alice.example.org", public=True)
    assert "ports" not in public["services"]["api"]
    assert public["networks"]["proxy"] == {"external": True, "name": "booking-proxy"}
    assert "proxy" in public["services"]["api"]["networks"]
    assert all(
        "proxy" not in public["services"][name]["networks"]
        for name in ("admin", "worker", "postgres", "redis")
    )
    labels = public["services"]["api"]["labels"]
    assert labels["traefik.enable"] == "true"
    assert "Host(`alice.example.org`)" in labels.values()
    assert public["services"]["admin"]["environment"]["TELEGRAM_WEBHOOK_BASE_URL"] == (
        "https://alice.example.org"
    )
    assert proxy.webhook_url("alice.example.org") == (
        "https://alice.example.org/api/v1/webhooks/telegram"
    )


def test_expose_wrong_dns_preserves_private_deployment(manager, monkeypatch):
    manager.create("alice", request())
    before = (manager.directory("alice") / "compose.yaml").read_bytes()
    monkeypatch.setattr(proxy, "domain_check", lambda *args: {"ok": False})
    with pytest.raises(DeploymentError, match="DNS"):
        manager.expose("alice", "alice.example.org")
    assert (manager.directory("alice") / "compose.yaml").read_bytes() == before
    assert manager.state("alice")["public"] is False


def test_expose_one_client_then_webhook(manager, monkeypatch):
    manager.create("alice", request())
    manager.create("bob", request("bob", TOKEN.replace("123456789", "123456780")))
    bob_before = (manager.directory("bob") / "compose.yaml").read_bytes()
    events = []
    monkeypatch.setattr(proxy, "domain_check", lambda *args: {"ok": True})
    monkeypatch.setattr(proxy, "status", lambda *args: True)
    monkeypatch.setattr(proxy, "config", lambda *args: {"staging": False})
    monkeypatch.setattr(
        proxy,
        "https_check",
        lambda *args, **kwargs: events.append("https") or {"live": True, "ready": True},
    )
    monkeypatch.setattr(manager, "set_webhook", lambda slug: events.append("webhook"))
    result = manager.expose("alice", "alice.example.org")
    assert result["webhook"] == "verified"
    assert events == ["https", "webhook"]
    assert manager.state("alice")["public"] is True
    assert (manager.directory("bob") / "compose.yaml").read_bytes() == bob_before
    assert all("bob" not in args for args in manager.calls)


def test_expose_tls_failure_restores_private_compose(manager, monkeypatch):
    manager.create("alice", request())
    before = (manager.directory("alice") / "compose.yaml").read_bytes()
    monkeypatch.setattr(proxy, "domain_check", lambda *args: {"ok": True})
    monkeypatch.setattr(proxy, "status", lambda *args: True)
    monkeypatch.setattr(proxy, "config", lambda *args: {"staging": False})
    monkeypatch.setattr(
        proxy,
        "https_check",
        lambda *args, **kwargs: {
            "live": False,
            "ready": False,
        },
    )
    with pytest.raises(DeploymentError, match="HTTPS"):
        manager.expose("alice", "alice.example.org")
    assert (manager.directory("alice") / "compose.yaml").read_bytes() == before
    assert manager.state("alice")["public"] is False


def test_webhook_requires_verified_public_https(manager, monkeypatch):
    manager.create("alice", request())
    with pytest.raises(DeploymentError, match="Public HTTPS"):
        manager.set_webhook("alice")
    state = manager.state("alice")
    state["domain"] = "alice.example.org"
    state["public"] = True
    manager.save("alice", state, "READY", "complete")
    monkeypatch.setattr(proxy, "config", lambda *args: {"staging": True})
    with pytest.raises(DeploymentError, match="trusted"):
        manager.set_webhook("alice")
    monkeypatch.setattr(proxy, "config", lambda *args: {"staging": False})
    monkeypatch.setattr(proxy, "domain_check", lambda *args: {"ok": True})
    monkeypatch.setattr(proxy, "status", lambda *args: True)
    monkeypatch.setattr(
        proxy,
        "https_check",
        lambda *args, **kwargs: {
            "live": False,
            "ready": False,
        },
    )
    with pytest.raises(DeploymentError, match="HTTPS"):
        manager.set_webhook("alice")
    assert not any("set-webhook" in args for args in manager.calls)
    monkeypatch.setattr(
        proxy,
        "https_check",
        lambda *args, **kwargs: {
            "live": True,
            "ready": True,
        },
    )
    manager.set_webhook("alice")
    assert manager.calls[-1][-2:] == ("booking-admin", "set-webhook")


def test_two_clients_isolated_and_idempotent(manager):
    first = manager.create("alice", request())
    second = manager.create("bob", request("bob", TOKEN.replace("123456789", "123456780")))
    assert first["status"] == second["status"] == "READY"
    assert first["project"] != second["project"]
    a, b = manager.values("alice"), manager.values("bob")
    assert all(a[key] != b[key] for key in module.SECRET_KEYS)
    assert a["BOOKING_IMAGE"] == b["BOOKING_IMAGE"]
    files = {p.name: p.read_bytes() for p in manager.directory("alice").iterdir()}
    manager.calls.clear()
    assert manager.create("alice") == first
    assert not manager.calls
    assert files == {p.name: p.read_bytes() for p in manager.directory("alice").iterdir()}
    config = load_specialist_template(manager.directory("bob") / "specialist.toml")
    assert config.profile.slug == "bob"
    assert config.services == request().template.services
    compose = json.loads((manager.directory("alice") / "compose.yaml").read_text())
    assert compose["networks"]["data"]["internal"]
    for service in ("postgres", "redis", "worker"):
        assert "ports" not in compose["services"][service]
    assert compose["services"]["api"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert TOKEN not in json.dumps(first) + json.dumps(compose)
    if os.name != "nt":
        assert (manager.directory("alice") / ".env").stat().st_mode & 0o777 == 0o600
        assert manager.directory("alice").stat().st_mode & 0o777 == 0o700


def test_same_bot_rejected(manager):
    manager.create("alice", request())
    with pytest.raises(DeploymentError, match="already belongs"):
        manager.create("bob", request("bob"))
    assert not manager.directory("bob").exists()


def test_migration_failure_preserves_secrets_and_resumes(manager, monkeypatch):
    original = manager.compose

    def fail(slug, *args, **kwargs):
        if "alembic" in args:
            raise DeploymentError("Migration failed")
        return original(slug, *args, **kwargs)

    monkeypatch.setattr(manager, "compose", fail)
    with pytest.raises(DeploymentError, match="Migration"):
        manager.create("alice", request())
    state = manager.state("alice")
    assert (state["status"], state["stage"]) == ("FAILED", "migration")
    before = manager.values("alice")
    assert not any("down" in args or "--volumes" in args for args in manager.calls)
    with pytest.raises(DeploymentError, match="resume"):
        manager.create("alice")
    monkeypatch.setattr(manager, "compose", original)
    assert manager.create("alice", resume=True)["status"] == "READY"
    assert manager.values("alice") == before
    assert manager.calls.index(("stop", "api", "worker")) < next(
        i for i, args in enumerate(manager.calls) if "alembic" in args
    )


def test_invalid_config_creates_nothing(manager):
    req = request()
    req.template = replace(
        req.template, profile=replace(req.template.profile, timezone="Invalid/TZ")
    )
    with pytest.raises(DeploymentError, match="configuration"):
        manager.create("alice", req)
    assert not manager.directory("alice").exists()


def test_docker_absent_creates_nothing(manager, monkeypatch):
    def fail():
        raise DeploymentError("Docker CLI not found")

    monkeypatch.setattr(module, "docker_preflight", fail)
    with pytest.raises(DeploymentError, match="Docker"):
        manager.create("alice", request())
    assert not manager.directory("alice").exists()


def test_lock_prevents_concurrent_mutation(tmp_path):
    root = tmp_path / "lock-root"
    with registry_lock(root), pytest.raises(DeploymentError, match="busy"), registry_lock(root):
        pytest.fail("Lock unexpectedly acquired twice")


def test_getme_validates_identity_and_hides_token(monkeypatch):
    body = {"ok": True, "result": {"id": 123456789, "is_bot": True, "username": "test_bot"}}
    monkeypatch.setattr(
        module.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(json.dumps(body).encode())
    )
    assert get_bot_identity(TOKEN) == BotIdentity(123456789, "test_bot")
    body["result"]["id"] += 1
    with pytest.raises(DeploymentError) as error:
        get_bot_identity(TOKEN)
    assert TOKEN not in str(error.value)


def test_invalid_token_api_response(monkeypatch):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError(
            f"https://api.telegram.org/bot{TOKEN}/getMe", 401, "Unauthorized", {}, None
        )

    monkeypatch.setattr(module.urllib.request, "urlopen", fail)
    with pytest.raises(DeploymentError, match="rejected") as error:
        get_bot_identity(TOKEN)
    assert TOKEN not in str(error.value)


def test_docker_output_not_leaked(monkeypatch):
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout=TOKEN, stderr=TOKEN),
    )
    with pytest.raises(DeploymentError) as error:
        run_docker(["info"])
    assert TOKEN not in str(error.value)


def test_environment_isolation(monkeypatch):
    for name in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "TELEGRAM_BOT_TOKEN", "BOOKING_IMAGE"):
        monkeypatch.setenv(name, "other-client")
    env = docker_environment({"TELEGRAM_BOT_TOKEN": TOKEN})
    assert "COMPOSE_FILE" not in env and "COMPOSE_PROJECT_NAME" not in env
    assert "BOOKING_IMAGE" not in env
    assert env["TELEGRAM_BOT_TOKEN"] == TOKEN


def test_still_running_admin_blocks_resume(manager, monkeypatch):
    monkeypatch.setattr(module, "run_docker", lambda *args, **kwargs: "old-admin-id")
    with pytest.raises(DeploymentError, match="admin container"):
        manager.create("alice", request())
    assert manager.state("alice")["status"] == "FAILED"
    assert not manager.calls


def test_logs_and_failed_admin_diagnostics_redact_secrets(manager, monkeypatch):
    manager.create("alice", request())
    values = manager.values("alice")
    raw = " ".join(values[key] for key in module.SECRET_KEYS) + " master_fakeinvite"
    monkeypatch.setattr(manager, "compose", lambda *args, **kwargs: raw)
    output = manager.logs("alice", None, 100)
    assert all(values[key] not in output for key in module.SECRET_KEYS)
    assert "fakeinvite" not in output

    def fail(*args, **kwargs):
        raise module.DockerCommandError(raw)

    monkeypatch.setattr(manager, "_compose", fail)
    with pytest.raises(DeploymentError, match="last-error.log"):
        DeploymentManager.compose(manager, "alice", "run", "admin")
    saved = (manager.directory("alice") / "last-error.log").read_text()
    assert all(values[key] not in saved for key in module.SECRET_KEYS)
    assert "fakeinvite" not in saved


def test_invalid_token_does_not_create_deployment(manager, monkeypatch):
    def invalid(token):
        raise DeploymentError("Telegram getMe rejected the token")

    monkeypatch.setattr(module, "get_bot_identity", invalid)
    with pytest.raises(DeploymentError, match="rejected"):
        manager.create("alice", request())
    assert not manager.directory("alice").exists()


def test_runtime_actions_do_not_migrate_or_issue_invites(manager):
    manager.create("alice", request())
    manager.calls.clear()
    for action in ("stop", "start", "restart"):
        manager.action("alice", action)
    assert all("run" not in args and "down" not in args for args in manager.calls)
    assert any("--wait" in args for args in manager.calls)


def test_cli_status_unhealthy_has_failure_exit(manager, monkeypatch, capsys):
    from booking_bot.deployment import cli

    manager.create("alice", request())
    monkeypatch.setattr(cli, "DeploymentManager", lambda root: manager)
    monkeypatch.setattr(manager, "runtime", lambda slug: [])
    args = cli.build_parser().parse_args(["status", "alice"])
    with pytest.raises(SystemExit) as error:
        cli.run(args)
    assert error.value.code == 1
    assert TOKEN not in capsys.readouterr().out


@pytest.fixture
def configurable(manager, monkeypatch):
    from booking_bot.deployment import configuration
    from booking_bot.deployment.templates import render_specialist

    manager.create("alice", request())
    monkeypatch.setattr(
        manager,
        "runtime",
        lambda slug: [
            {"service": service, "state": "running", "health": "healthy"}
            for service in module.SERVICES
        ],
    )
    operations = []

    def operation(manager, slug, name, snapshot=None):
        operations.append(name)
        return {"ok": True}

    monkeypatch.setattr(configuration, "runtime_operation", operation)
    candidate = manager.root.parent / "candidate.toml"
    template = request().template
    candidate.write_text(
        render_specialist(
            replace(template, profile=replace(template.profile, brand_name="New brand"))
        ),
        encoding="utf-8",
    )
    return manager, candidate, operations


def test_configure_recreates_mounts_and_keeps_identity(configurable):
    manager, candidate, operations = configurable
    values = manager.values("alice")
    manager.configure("alice", candidate)
    assert operations == ["snapshot", "apply"]
    assert manager.state("alice")["status"] == "READY"
    assert manager.values("alice") == values
    assert (
        load_specialist_template(manager.directory("alice") / "specialist.toml").profile.brand_name
        == "New brand"
    )
    assert any("--force-recreate" in args for args in manager.calls)


def test_configure_rejects_catalog_change_before_mutating(configurable):
    manager, candidate, operations = configurable
    candidate.write_text(
        candidate.read_text(encoding="utf-8").replace(
            '"duration_minutes" = 60', '"duration_minutes" = 90'
        ),
        encoding="utf-8",
    )
    before = (manager.directory("alice") / "specialist.toml").read_bytes()
    with pytest.raises(DeploymentError, match="Services/schedule"):
        manager.configure("alice", candidate)
    assert (manager.directory("alice") / "specialist.toml").read_bytes() == before
    assert not operations


def test_configure_failed_health_rolls_back(configurable, monkeypatch):
    manager, candidate, operations = configurable
    before = (manager.directory("alice") / "specialist.toml").read_bytes()
    original = manager.compose
    failed = False

    def fail(slug, *args, **kwargs):
        nonlocal failed
        if "--force-recreate" in args and not failed:
            failed = True
            raise DeploymentError("Health failed")
        return original(slug, *args, **kwargs)

    monkeypatch.setattr(manager, "compose", fail)
    with pytest.raises(DeploymentError, match="restored"):
        manager.configure("alice", candidate)
    assert (manager.directory("alice") / "specialist.toml").read_bytes() == before
    assert operations == ["snapshot", "apply", "restore"]
    assert manager.state("alice")["status"] == "READY"


def test_interrupted_configure_requires_its_own_resume(configurable, monkeypatch):
    from booking_bot.deployment import configuration

    manager, candidate, operations = configurable
    original = configuration.runtime_operation

    def fail(manager, slug, operation, snapshot=None):
        if operation in {"apply", "restore"}:
            raise DeploymentError("Docker unavailable")
        return original(manager, slug, operation, snapshot)

    monkeypatch.setattr(configuration, "runtime_operation", fail)
    with pytest.raises(DeploymentError, match="configure SLUG --resume"):
        manager.configure("alice", candidate)
    assert manager.state("alice")["status"] == "FAILED"
    with pytest.raises(DeploymentError, match="configure SLUG --resume"):
        manager.create("alice", resume=True)
    with pytest.raises(DeploymentError):
        manager.action("alice", "start")
    monkeypatch.setattr(configuration, "runtime_operation", original)
    manager.configure("alice", resume=True)
    assert manager.state("alice")["status"] == "READY"
    assert (
        load_specialist_template(manager.directory("alice") / "specialist.toml").profile.brand_name
        == request().template.profile.brand_name
    )


def test_configure_stopped_installation_stays_stopped(configurable, monkeypatch):
    manager, candidate, _ = configurable
    monkeypatch.setattr(manager, "runtime", lambda slug: [])
    manager.calls.clear()
    manager.configure("alice", candidate)
    assert any("--no-start" in args and "--force-recreate" in args for args in manager.calls)
    assert not any(
        args[0] == "up" and "api" in args and "--no-start" not in args for args in manager.calls
    )
    assert manager.calls[-1] == ("stop", "redis", "postgres")


def test_file_generation_resume_preserves_initial_secrets(manager, monkeypatch):
    original = module.atomic_write

    def fail(path, content, **kwargs):
        if path.name == "specialist.toml":
            raise OSError("Disk temporarily unavailable")
        return original(path, content, **kwargs)

    monkeypatch.setattr(module, "atomic_write", fail)
    with pytest.raises(OSError):
        manager.create("alice", request())
    before = manager.values("alice")
    assert manager.state("alice")["stage"] == "files"
    monkeypatch.setattr(module, "atomic_write", original)
    manager.create("alice", resume=True)
    assert manager.values("alice") == before
    assert manager.state("alice")["status"] == "READY"


def test_doctor_missing_and_list_partial_registry(tmp_path):
    manager = DeploymentManager(tmp_path)
    report = manager.doctor("missing")
    assert not report["ok"] and "does not exist" in report["checks"][0]["detail"]
    (tmp_path / "unfinished").mkdir()
    assert manager.list() == [
        {
            "slug": "unfinished",
            "status": "FAILED",
            "stage": "state",
            "diagnostic": "Incomplete/corrupt state; run doctor",
        }
    ]


def test_doctor_stopped_never_starts_services(manager, monkeypatch):
    manager.create("alice", request())
    monkeypatch.setattr(manager, "runtime", lambda slug: [])
    manager.calls.clear()
    report = manager.doctor("alice")
    assert not report["ok"]
    assert all("up" not in args and "run" not in args for args in manager.calls)
    assert next(c for c in report["checks"] if c["check"] == "migrations")["ok"] is False


def test_corrupt_state_types_produce_diagnostic(manager):
    manager.create("alice", request())
    path = manager.directory("alice") / "state.json"
    state = json.loads(path.read_text())
    state["project"] = ["bad"]
    path.write_text(json.dumps(state))
    report = manager.doctor("alice")
    assert not report["ok"]
    assert "Invalid deployment state" in report["checks"][0]["detail"]


def test_refresh_legacy_compose_preserves_storage_and_refuses_custom_model(manager):
    from booking_bot.deployment.files import atomic_write
    from booking_bot.deployment.templates import compose_model, legacy_compose_model

    manager.create("alice", request())
    state = manager.state("alice")
    path = manager.directory("alice") / "compose.yaml"
    legacy = legacy_compose_model(state["project"])
    atomic_write(path, json.dumps(legacy))
    manager.refresh_compose("alice")
    expected = compose_model(state["project"])
    assert json.loads(path.read_text()) == expected
    assert json.loads(path.with_name("compose-previous.json").read_text()) == legacy
    assert expected["volumes"] == legacy["volumes"]
    assert expected["networks"] == legacy["networks"]
    modified = dict(expected)
    modified["volumes"] = {"someone_elses_database": {}}
    atomic_write(path, json.dumps(modified))
    with pytest.raises(DeploymentError, match="known templates"):
        manager.refresh_compose("alice")
    assert json.loads(path.read_text()) == modified

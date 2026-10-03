import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from booking_bot.config import Settings
from booking_bot.deployment import alerts, monitoring
from booking_bot.deployment.cli import build_parser, run_monitoring
from booking_bot.deployment.files import DeploymentError, registry_lock
from booking_bot.deployment.monitor_schedule import units
from booking_bot.deployment.templates import LOGGING, compose_model, legacy_compose_model
from booking_bot.logging_config import SafeFormatter


def report(severity="ERROR", *, observed=True):
    return {
        "slug": "alice",
        "observed": observed,
        "checks": [{"check": "api", "severity": severity}],
        "ok": severity in {"OK", "WARNING"},
    }


@pytest.mark.parametrize(
    "percent,expected",
    [(79, "OK"), (80, "WARNING"), (90, "ERROR"), (95, "CRITICAL"), (97, "CRITICAL")],
)
def test_disk_thresholds(percent, expected):
    assert monitoring.disk_level(percent, monitoring.Thresholds()) == expected


@pytest.mark.parametrize(
    "name,value",
    [
        ("DISK_WARNING", "nan"),
        ("DISK_ERROR", "79"),
        ("DISK_CRITICAL", "101"),
        ("BACKUP_ERROR_HOURS", "12"),
        ("CERTIFICATE_ERROR_DAYS", "40"),
        ("MEMORY_ERROR", "84"),
        ("FAILED_JOBS_ERROR", "-1"),
    ],
)
def test_invalid_thresholds_fail_closed(monkeypatch, name, value):
    monkeypatch.setenv("BOOKING_MONITOR_" + name, value)
    with pytest.raises(DeploymentError, match="thresholds"):
        monitoring.Thresholds.environment()


def test_alerts_persist_dedup_and_partial_recovery(tmp_path):
    messages = []

    def sender(message):
        messages.append(message)
        return True

    assert alerts.reconcile(tmp_path, [report()], now=100, sender=sender)["sent"] == 1
    assert alerts.reconcile(tmp_path, [report()], now=101, sender=sender)["sent"] == 0
    # A deferred probe cannot falsely report recovery during backup/update.
    assert (
        alerts.reconcile(tmp_path, [report("OK", observed=False)], now=102, sender=sender)["sent"]
        == 0
    )
    assert alerts.reconcile(tmp_path, [report("OK")], now=103, sender=sender)["sent"] == 1
    assert "RECOVERED: api" in messages[-1]
    assert alerts.reconcile(tmp_path, [report("OK")], now=104, sender=sender)["sent"] == 0


def test_failed_alert_delivery_is_retried(tmp_path):
    def fail(_):
        raise DeploymentError("network unavailable")

    assert alerts.reconcile(tmp_path, [report()], now=1, sender=fail)["delivery_failures"] == 1
    assert alerts.reconcile(tmp_path, [report()], now=2, sender=lambda _: True)["sent"] == 1
    assert alerts.reconcile(tmp_path, [report()], now=3, sender=lambda _: True)["sent"] == 0


def test_disabled_alerts_do_not_acknowledge_pending(tmp_path):
    assert alerts.reconcile(tmp_path, [report()], now=1, sender=lambda _: False)["alerts_disabled"]
    assert alerts.reconcile(tmp_path, [report()], now=2, sender=lambda _: True)["sent"] == 1


def test_severity_change_not_delayed_by_cooldown(tmp_path):
    alerts.reconcile(tmp_path, [report()], now=1, sender=lambda _: True)
    assert (
        alerts.reconcile(tmp_path, [report("CRITICAL")], now=2, sender=lambda _: True)["sent"] == 1
    )


def test_telegram_sender_never_exposes_token(monkeypatch):
    token = "123456789:" + "secret" * 6
    monkeypatch.setenv("MONITORING_TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("MONITORING_TELEGRAM_CHAT_ID", "1234")

    def failure(*args, **kwargs):
        raise RuntimeError(token)

    monkeypatch.setattr(alerts.urllib.request, "urlopen", failure)
    with pytest.raises(DeploymentError) as caught:
        alerts.send_telegram("only service identifiers")
    assert token not in str(caught.value)


def test_lock_probe_does_not_change_file(tmp_path):
    with registry_lock(tmp_path):
        size = (tmp_path / ".lock").stat().st_size
        assert monitoring.lock_busy(tmp_path / ".lock")
        assert (tmp_path / ".lock").stat().st_size == size
    assert not monitoring.lock_busy(tmp_path / ".lock")
    assert not monitoring.lock_busy(tmp_path / "missing")


def test_systemd_monitor_uses_private_environment_and_escaped_paths():
    service, timer = units(Path("/srv/with space/$x%y"), Path("/backups"), "/venv/bookingctl")
    assert '"monitor"' in service and "backup-all" not in service
    assert "/etc/booking-monitor.env" in service and "UMask=0077" in service
    assert "$$x%%y" in service
    assert "OnUnitInactiveSec=5min" in timer


def test_application_logs_have_fields_without_library_exception_payloads():
    settings = Settings(_env_file=None).model_copy(update={"app_env": "production"})
    formatter = SafeFormatter(settings, "alice")
    record = logging.LogRecord(
        "aiogram.event",
        logging.ERROR,
        __file__,
        1,
        "Alice +79991234567 private message",
        (),
        (RuntimeError, RuntimeError("private message"), None),
    )
    event = json.loads(formatter.format(record))
    assert event["deployment_slug"] == "alice" and event["service"] == "application"
    assert "Alice" not in json.dumps(event) and "private message" not in json.dumps(event)
    assert event["event"] == "Unhandled library exception"


def test_generated_models_rotation_restart_and_isolation():
    for public in (False, True):
        model = compose_model("booking-alice-deadbeef", "alice.example.org", public=public)
        for name, service in model["services"].items():
            assert service["logging"] == LOGGING
            assert service["restart"] == ("no" if name == "admin" else "unless-stopped")
            assert all("docker.sock" not in str(mount) for mount in service.get("volumes", []))
        for name in ("postgres", "redis"):
            assert model["services"][name]["networks"] == ["data"]
            assert not model["services"][name].get("ports")
        assert model["networks"]["data"]["internal"]
        legacy = legacy_compose_model("booking-alice-deadbeef", "alice.example.org", public=public)
        assert legacy["services"]["worker"]["restart"] == "on-failure:5"
        assert "logging" not in legacy["services"]["postgres"]


def test_socket_proxy_is_not_on_client_network():
    from booking_bot.deployment.proxy import proxy_compose

    services = proxy_compose(False)["services"]
    proxy = services["socket-proxy"]
    assert proxy["environment"]["POST"] == "0" and not proxy.get("ports")
    assert proxy["networks"] == ["docker-api"]
    assert not any("docker.sock" in mount for mount in services["traefik"]["volumes"])


@pytest.mark.parametrize(
    "severity,observed,ready",
    [
        ("OK", True, True),
        ("WARNING", True, True),
        ("ERROR", True, False),
        ("CRITICAL", True, False),
        ("OK", False, False),
    ],
)
def test_acceptance_never_claims_ready_with_errors_or_deferred_checks(
    monkeypatch,
    capsys,
    severity,
    observed,
    ready,
):
    result = report(severity, observed=observed)
    result["checks"][0]["detail"] = "fixture"
    monkeypatch.setattr(monitoring, "client_report", lambda *a, **k: result)
    monkeypatch.setattr(monitoring, "host_report", lambda *a, **k: {"checks": []})
    args = build_parser().parse_args(["production-check", "alice"])
    if ready:
        run_monitoring(args, None)
    else:
        with pytest.raises(SystemExit):
            run_monitoring(args, None)
    output = capsys.readouterr().out
    assert ("RESULT: PRODUCTION READY" in output) == ready


def test_backup_freshness_is_lightweight_and_rejects_wrong_client(tmp_path, monkeypatch):
    root = tmp_path / "clients"
    backups = tmp_path / "backups"
    manager = SimpleNamespace(root=root)
    directory = backups / "alice" / "20261001T120000Z-deadbeef"
    directory.mkdir(parents=True)
    from booking_bot.deployment.backup import ALL_FILES

    for name in ALL_FILES:
        (directory / name).write_text("fixture")
    manifest = {
        "client_slug": "alice",
        "created_at": (datetime.now(UTC) - timedelta(hours=30)).isoformat(),
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    result = monitoring.backup_summary(manager, "alice", backups)
    assert result["age_hours"] >= 30 and result["size_bytes"] > 0
    assert result["latest"] == directory.name
    manifest["client_slug"] = "bob"
    (directory / "manifest.json").write_text(json.dumps(manifest))
    result = monitoring.backup_summary(manager, "alice", backups)
    assert result["invalid"] == 1 and result["latest"] is None


def test_parser_optional_slugs_and_log_follow():
    parser = build_parser()
    assert parser.parse_args(["status"]).slug is None
    assert parser.parse_args(["doctor"]).slug is None
    assert parser.parse_args(["logs", "alice", "--follow", "--service", "worker"]).follow


def test_partial_check_deferred_does_not_emit_false_recovery(tmp_path):
    alerts.reconcile(tmp_path, [report()], now=1, sender=lambda _: True)
    deferred = report("WARNING")
    deferred["checks"][0]["observed"] = False
    assert alerts.reconcile(tmp_path, [deferred], now=2, sender=lambda _: True)["sent"] == 0


def test_production_json_is_machine_readable(monkeypatch, capsys):
    result = report("OK")
    monkeypatch.setattr(monitoring, "client_report", lambda *a, **k: result)
    monkeypatch.setattr(monitoring, "host_report", lambda *a, **k: {"checks": []})
    run_monitoring(build_parser().parse_args(["production-check", "alice", "--json"]), None)
    assert json.loads(capsys.readouterr().out)["result"] == "PRODUCTION READY"


@pytest.mark.parametrize(
    "days,expected", [(60, "OK"), (14, "WARNING"), (3, "ERROR"), (-1, "CRITICAL")]
)
def test_certificate_expiry_thresholds(monkeypatch, days, expected):
    expires = (datetime.now(UTC) + timedelta(days=days)).strftime("%b %d %H:%M:%S %Y GMT")

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def getpeercert(self):
            return {"notAfter": expires, "issuer": ((("organizationName", "Test CA"),),)}

    monkeypatch.setattr(monitoring.socket, "create_connection", lambda *a, **k: Connection())
    monkeypatch.setattr(
        monitoring.ssl,
        "create_default_context",
        lambda: SimpleNamespace(wrap_socket=lambda *a, **k: Connection()),
    )
    assert (
        monitoring.certificate("alice.example.org", monitoring.Thresholds())["severity"] == expected
    )

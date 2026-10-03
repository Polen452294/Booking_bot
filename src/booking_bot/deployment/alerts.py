"""Independent monitoring bot, persistent delivery state and recovery notifications."""

import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from booking_bot.deployment.files import (
    DeploymentError,
    atomic_write,
    private_directory,
    read_json,
    registry_lock,
)

WARNING_ALERTS = {"backup", "tls_expiry", "notifications", "disk", "backup_disk", "docker_disk"}


def send_telegram(message: str) -> bool:
    token = os.environ.get("MONITORING_TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("MONITORING_TELEGRAM_CHAT_ID", "")
    if not token and not chat:
        return False
    if not re.fullmatch(r"\d{5,16}:[A-Za-z0-9_-]{20,}", token) or not re.fullmatch(r"-?\d+", chat):
        raise DeploymentError("Invalid monitoring Telegram configuration; secrets omitted")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urllib.parse.urlencode({"chat_id": chat, "text": message[:4000]}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise ValueError
        return True
    except Exception:
        # URL/HTTP exceptions contain the bot token. Never propagate them to output.
        raise DeploymentError("Monitoring alert delivery failed; pending alert retained") from None


def reconcile(
    path: Path, reports: list[dict], *, now: float | None = None, sender=send_telegram
) -> dict:
    now = time.time() if now is None else now
    try:
        cooldown = float(os.environ.get("BOOKING_MONITOR_COOLDOWN_SECONDS", "21600"))
        if not 60 <= cooldown <= 30 * 86400:
            raise ValueError
    except ValueError:
        raise DeploymentError("Invalid BOOKING_MONITOR_COOLDOWN_SECONDS (60..2592000)") from None
    private_directory(path)
    with registry_lock(path):
        file = path / "alerts.json"
        state = read_json(file) if file.exists() else {"format": 1, "scopes": {}}
        if state.get("format") != 1 or not isinstance(state.get("scopes"), dict):
            raise DeploymentError("Invalid alert state; restore private monitoring state")
        sent, failures, disabled = 0, 0, False
        for report in reports:
            if not report.get("observed", True):
                continue
            scope = report["slug"]
            previous = state["scopes"].get(scope, {"notified": {}, "sent_at": 0})
            issues = {
                c["check"]: c["severity"]
                for c in report["checks"]
                if c["severity"] in {"ERROR", "CRITICAL"}
                or c["severity"] == "WARNING"
                and c["check"] in WARNING_ALERTS
            }
            for check in report["checks"]:
                name = check["check"]
                if not check.get("observed", True) and name in previous.get("current", {}):
                    issues[name] = previous["current"][name]
            notified = previous["notified"]
            previous["current"] = issues
            # Persist observation before networking. Unsent alerts are retried on the next pass.
            state["scopes"][scope] = previous
            atomic_write(file, json.dumps(state, indent=2))
            changed = issues != notified
            if not changed and not (issues and now - previous["sent_at"] >= cooldown):
                continue
            if not issues and not notified:
                continue
            recovered = sorted(set(notified) - set(issues))
            lines = [f"Booking monitoring: {scope}", "RECOVERED" if not issues else "ATTENTION"]
            lines.extend(f"{level}: {name}" for name, level in sorted(issues.items()))
            lines.extend(f"RECOVERED: {name}" for name in recovered)
            try:
                delivered = sender("\n".join(lines))
            except DeploymentError:
                failures += 1
                continue
            if delivered:
                previous.update(notified=issues, sent_at=now)
                atomic_write(file, json.dumps(state, indent=2))
                sent += 1
            else:
                disabled = True
        return {
            "sent": sent,
            "delivery_failures": failures,
            "alerts_disabled": disabled,
            "active_scopes": sum(bool(s.get("current")) for s in state["scopes"].values()),
        }


def operation_event(
    manager, slug: str, kind: str, *, failed: bool, backup_root: Path | None = None
):
    """Durable error latch cleared only by a later successful operation of the same kind."""
    from datetime import UTC, datetime

    from booking_bot.deployment.backup import BackupManager

    if kind not in {"backup", "restore", "update", "rollback"}:
        return
    manager.state(slug)
    backups = BackupManager(manager, backup_root)
    private_directory(backups.root)
    directory = backups.directory(slug)
    private_directory(directory)
    with registry_lock(directory):
        file = directory / "operations.json"
        events = read_json(file) if file.exists() else {}
        events[kind] = {"failed": failed, "at": datetime.now(UTC).isoformat()}
        if not failed and kind in {"restore", "rollback"}:
            for recovered in ("update", "restore", "rollback"):
                if recovered in events and events[recovered]["failed"]:
                    events[recovered] = {
                        "failed": False,
                        "at": datetime.now(UTC).isoformat(),
                        "recovered_by": kind,
                    }
        atomic_write(file, json.dumps(events))


def monitor(manager, backup_root: Path | None = None) -> dict:
    from booking_bot.deployment.monitoring import all_reports, host_report

    directory = manager.root.parent / "monitoring"
    private_directory(directory)
    # Separate pass lock: no registry lock is held during read-only/network probes.
    with registry_lock(directory / "pass"):
        reports = [host_report(manager, backup_root), *all_reports(manager, backup_root)]
        delivery = reconcile(directory, reports)
        atomic_write(
            directory / "last-pass.json",
            json.dumps(
                {
                    "at": time.time(),
                    "reports": reports,
                    "delivery": delivery,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    return {
        "reports": reports,
        "delivery": delivery,
        "ok": all(report["ok"] for report in reports) and not delivery["delivery_failures"],
    }

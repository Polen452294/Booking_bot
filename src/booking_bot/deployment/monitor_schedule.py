"""Systemd one-shot monitoring every five minutes, separate private credentials."""

import os
import shutil
import subprocess
from pathlib import Path

from booking_bot.deployment.backup_schedule import units as backup_units
from booking_bot.deployment.files import DeploymentError, atomic_write


def units(root: Path, backups: Path, executable: str) -> tuple[str, str]:
    # Reuse the existing tested systemd argument/path escaping.
    service, _ = backup_units(root, backups, executable, "03:00")
    service = (
        service.replace("Booking client backups", "Booking production monitoring")
        .replace("/etc/booking-backup.env", "/etc/booking-monitor.env")
        .replace('"backup-all" "--retention"', '"monitor"')
    )
    timer = (
        "[Unit]\nDescription=Booking monitoring every five minutes\n[Timer]\n"
        "OnBootSec=2min\nOnUnitInactiveSec=5min\nRandomizedDelaySec=30\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def schedule(action: str, root: Path, backups: Path):
    if os.name != "posix" or not shutil.which("systemctl"):
        raise DeploymentError("Monitoring scheduling requires a Linux systemd host")
    if action == "install":
        executable = shutil.which("bookingctl")
        if not executable:
            raise DeploymentError("Install bookingctl in a persistent host virtualenv first")
        service, timer = units(root, backups, executable)
        for name, content in (("service", service), ("timer", timer)):
            atomic_write(Path(f"/etc/systemd/system/booking-monitor.{name}"), content)
        commands = [["daemon-reload"], ["enable", "--now", "booking-monitor.timer"]]
    else:
        commands = [
            ["status", "booking-monitor.timer", "--no-pager"],
            ["show", "booking-monitor.service", "-p", "Result", "-p", "ExecMainStatus"],
        ]
    for command in commands:
        if subprocess.run(["systemctl", *command], timeout=30).returncode:
            raise DeploymentError("Systemd monitor timer operation failed")

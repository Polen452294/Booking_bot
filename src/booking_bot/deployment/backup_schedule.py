"""Host-level systemd scheduling; secrets remain in a separate private EnvironmentFile."""

import os
import re
import shutil
import subprocess
from pathlib import Path

from booking_bot.deployment.files import DeploymentError, atomic_write


def units(root: Path, backups: Path, executable: str, at: str) -> tuple[str, str]:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at):
        raise DeploymentError("Schedule time must be HH:MM UTC")

    def quote(value):
        if any(c in str(value) for c in ("\n", "\r", "\x00")):
            raise DeploymentError("Invalid systemd path")
        return (
            '"'
            + str(value)
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("%", "%%")
            .replace("$", "$$")
            + '"'
        )

    command = " ".join(
        quote(v)
        for v in (executable, "--root", root, "--backup-root", backups, "backup-all", "--retention")
    )
    service = (
        "[Unit]\nDescription=Booking client backups\nAfter=docker.service network-online.target\n"
        "[Service]\nType=oneshot\nUMask=0077\n"
        "EnvironmentFile=-/etc/booking-backup.env\n"
        f"ExecStart={command}\nTimeoutStartSec=infinity\n"
    )
    timer = (
        "[Unit]\nDescription=Daily Booking backup\n[Timer]\n"
        f"OnCalendar=*-*-* {at}:00 UTC\nPersistent=true\nRandomizedDelaySec=300\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def schedule(action: str, root: Path, backups: Path, at: str) -> None:
    if os.name != "posix" or not shutil.which("systemctl"):
        raise DeploymentError("Backup scheduling requires a Linux systemd host")
    if action == "install":
        executable = shutil.which("bookingctl")
        if not executable:
            raise DeploymentError("Install bookingctl in a persistent host virtualenv first")
        service, timer = units(root, backups, executable, at)
        for name, content in (("service", service), ("timer", timer)):
            atomic_write(Path(f"/etc/systemd/system/booking-backup.{name}"), content)
        commands = [["daemon-reload"], ["enable", "--now", "booking-backup.timer"]]
    else:
        commands = [
            ["status", "booking-backup.timer", "--no-pager"],
            ["show", "booking-backup.service", "-p", "Result", "-p", "ExecMainStatus"],
        ]
    for command in commands:
        if subprocess.run(["systemctl", *command], timeout=30).returncode:
            raise DeploymentError("systemd backup timer operation failed")

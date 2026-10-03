"""Install a persistent operator CLI without altering existing client deployments."""

import argparse
import json
import os
import runpy
import subprocess
import tempfile
from pathlib import Path

ROOT = Path("/opt/booking")
WRAPPER = Path("/usr/local/bin/bookingctl")


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True, timeout=900)


def private_dir(path):
    # Refuse links in every ancestor, including a redirected /opt/booking.
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("Bootstrap paths must not contain symlinks")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def write(path, content, mode=0o600):
    if path.is_symlink():
        raise ValueError("Bootstrap files must not be symlinks")
    fd, filename = tempfile.mkstemp(prefix=".bootstrap-", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def bootstrap(source, *, email=None, ipv4=None, ipv6=None, staging=False, prepare_only=False):
    source = source.resolve(strict=True)
    version = runpy.run_path(str(source / "src/booking_bot/version.py"))["__version__"]
    for name in ("", "bin", "infra", "clients", "backups", "state", "logs"):
        private_dir(ROOT / name)
    marker = ROOT / "state/bootstrap.json"
    if marker.is_symlink():
        raise ValueError("Bootstrap state must not be a symlink")
    installed = json.loads(marker.read_text()) if marker.exists() else None
    if installed and installed["version"] != version:
        raise ValueError("Existing CLI has another version; use an explicit operator CLI upgrade")
    python = ROOT / "venv/bin/python"
    executable = ROOT / "venv/bin/bookingctl"
    if not installed:
        private_dir(ROOT / "venv")
        run("python3.12", "-m", "venv", ROOT / "venv")
        run(
            python,
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "-r",
            source / "requirements-dev.lock",
            "-r",
            source / "requirements-build.lock",
        )
        run(python, "-m", "pip", "install", "--no-deps", "--no-build-isolation", source)
        run(python, "-m", "pip", "check")
    template = ROOT / "infra/specialist.toml"
    if template.is_symlink():
        raise ValueError("Bootstrap template must not be a symlink")
    if not template.exists():
        write(template, (source / "specialist.toml").read_text(encoding="utf-8"))
    wrapper = (
        "#!/bin/sh\nset -eu\numask 077\n"
        f"cd '{ROOT / 'infra'}'\n"
        f"exec '{executable}' --root '{ROOT / 'clients'}' "
        f"--backup-root '{ROOT / 'backups'}' \"$@\"\n"
    )
    if WRAPPER.exists() and (WRAPPER.is_symlink() or WRAPPER.read_text() != wrapper):
        raise ValueError("Existing bookingctl launcher differs; review it before installation")
    write(ROOT / "bin/bookingctl", wrapper, 0o700)
    write(WRAPPER, wrapper, 0o755)
    run(WRAPPER, "version")
    if not installed:
        write(marker, json.dumps({"version": version}) + "\n")
    if prepare_only:
        print("CLI prepared; Docker/proxy/systemd/VPS qualification NOT performed.")
        return
    settings_file = ROOT / "infra/proxy/settings.json"
    if settings_file.is_symlink():
        raise ValueError("Proxy settings must not be a symlink")
    if settings_file.exists():
        settings = json.loads(settings_file.read_text())
        if (
            any(
                value is not None and settings[key] != value
                for key, value in (
                    ("email", email),
                    ("server_ipv4", ipv4),
                    ("server_ipv6", ipv6),
                )
            )
            or settings["staging"] != staging
        ):
            raise ValueError(
                "Existing proxy settings differ; use explicit proxy configuration commands"
            )
    else:
        if not email or not (ipv4 or ipv6):
            raise ValueError(
                "First bootstrap needs --acme-email and --server-ipv4 or --server-ipv6"
            )
        args = ["proxy", "init", "--email", email]
        if ipv4:
            args += ["--server-ipv4", ipv4]
        if ipv6:
            args += ["--server-ipv6", ipv6]
        if staging:
            args += ["--staging"]
        run(WRAPPER, *args)
    run(WRAPPER, "proxy", "start")
    run(WRAPPER, "backup", "schedule", "install")
    run(WRAPPER, "monitor", "--schedule", "install")
    run(WRAPPER, "doctor")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--acme-email")
    parser.add_argument("--server-ipv4")
    parser.add_argument("--server-ipv6")
    parser.add_argument("--staging", action="store_true")
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Install CLI only; does not qualify Docker, systemd, proxy or VPS",
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run bootstrap as root")
    bootstrap(
        args.source,
        email=args.acme_email,
        ipv4=args.server_ipv4,
        ipv6=args.server_ipv6,
        staging=args.staging,
        prepare_only=args.prepare_only,
    )

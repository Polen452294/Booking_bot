"""Safe one-shot operational diagnostics. No service starts, migrations or public metrics."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import socket
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from booking_bot.deployment.files import DeploymentError, private_permissions, read_json

if TYPE_CHECKING:
    from booking_bot.deployment.manager import DeploymentManager

LEVELS = {"OK": 0, "WARNING": 1, "ERROR": 2, "CRITICAL": 3}


@dataclass(frozen=True)
class Thresholds:
    disk_warning: float = 80
    disk_error: float = 90
    disk_critical: float = 95
    backup_warning_hours: float = 24
    backup_error_hours: float = 48
    certificate_warning_days: float = 30
    certificate_error_days: float = 7
    failed_jobs_error: float = 10
    memory_warning: float = 85
    memory_error: float = 95
    load_warning: float = 2

    @classmethod
    def environment(cls) -> Thresholds:
        try:
            values = {
                key: float(os.environ.get(f"BOOKING_MONITOR_{key.upper()}", default))
                for key, default in cls().__dict__.items()
            }
            if any(not math.isfinite(value) or value <= 0 for value in values.values()):
                raise ValueError
            if (
                not 0
                < values["disk_warning"]
                < values["disk_error"]
                < values["disk_critical"]
                < 100
            ):
                raise ValueError
            if not values["backup_warning_hours"] < values["backup_error_hours"]:
                raise ValueError
            if not values["certificate_error_days"] < values["certificate_warning_days"]:
                raise ValueError
            if not values["memory_warning"] < values["memory_error"] < 100:
                raise ValueError
            return cls(**values)
        except ValueError:
            raise DeploymentError("Invalid BOOKING_MONITOR thresholds") from None


def add(report: dict, name: str, severity: str, detail: str) -> None:
    report["checks"].append(
        {
            "check": name,
            "severity": severity,
            "ok": LEVELS[severity] < LEVELS["ERROR"],
            "detail": detail,
        }
    )


def finish(report: dict) -> dict:
    report["severity"] = max(
        (check["severity"] for check in report["checks"]), key=LEVELS.get, default="ERROR"
    )
    report["ok"] = bool(report["checks"]) and LEVELS[report["severity"]] < LEVELS["ERROR"]
    return report


def disk_level(used_percent: float, limits: Thresholds) -> str:
    for level, threshold in (
        ("CRITICAL", limits.disk_critical),
        ("ERROR", limits.disk_error),
        ("WARNING", limits.disk_warning),
    ):
        if used_percent >= threshold:
            return level
    return "OK"


def disk_check(report: dict, name: str, path: Path, limits: Thresholds) -> None:
    try:
        while not path.exists() and path != path.parent:
            path = path.parent
        usage = shutil.disk_usage(path)
        percent = usage.used / usage.total * 100
        add(
            report,
            name,
            disk_level(percent, limits),
            f"{percent:.1f}% used; {usage.free} bytes free",
        )
    except OSError:
        add(report, name, "ERROR", "Cannot inspect filesystem usage")


def lock_busy(path: Path) -> bool:
    """Probe an existing advisory lock, never create or rewrite it."""
    if not path.exists():
        return False
    if path.is_symlink() or path.is_junction():
        raise DeploymentError("Lock file must not be a link")
    try:
        with path.open("r+b") as stream:
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return True
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return False
    except OSError:
        raise DeploymentError("Cannot inspect operation lock") from None


def backup_summary(manager: DeploymentManager, slug: str, backup_root: Path | None) -> dict:
    from booking_bot.deployment.backup import ALL_FILES, ID_PATTERN, BackupManager, safe_path

    backups = BackupManager(manager, backup_root)
    directory = backups.directory(slug)
    result = {
        "latest": None,
        "oldest": None,
        "age_hours": None,
        "size_bytes": 0,
        "invalid": 0,
        "incomplete": 0,
        "integrity": "metadata-only; use backup verify for full checksum/archive check",
    }
    rows = []
    if not directory.exists():
        return result
    for path in directory.iterdir():
        if path.name.startswith(".incomplete-"):
            result["incomplete"] += 1
        elif not re.fullmatch(ID_PATTERN, path.name):
            continue
        try:
            safe_path(path)
            # Only the known files, no recursive volume/FS walk and no reading large dumps.
            for name in ALL_FILES:
                file = safe_path(path / name)
                if file.exists():
                    result["size_bytes"] += file.stat().st_size
            if path.name.startswith(".incomplete-"):
                continue
            manifest = read_json(path / "manifest.json")
            created = datetime.fromisoformat(manifest["created_at"])
            age = (datetime.now(UTC) - created).total_seconds() / 3600
            if (
                manifest["client_slug"] != slug
                or created.tzinfo is None
                or age < 0
                or any(not (path / name).is_file() for name in ALL_FILES)
            ):
                raise ValueError
            rows.append((created, path.name, age))
        except (OSError, DeploymentError, ValueError, KeyError, TypeError):
            result["invalid"] += 1
    if rows:
        rows.sort()
        result.update(
            latest=rows[-1][1],
            oldest=rows[0][1],
            age_hours=round(rows[-1][2], 2),
            created_at=rows[-1][0].isoformat(),
        )
    return result


def certificate(domain: str, limits: Thresholds) -> dict:
    from booking_bot.deployment.manager import validate_domain

    validate_domain(domain)
    with socket.create_connection((domain, 443), timeout=5) as connection:
        with ssl.create_default_context().wrap_socket(connection, server_hostname=domain) as tls:
            cert = tls.getpeercert()
    expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), UTC)
    days = (expires - datetime.now(UTC)).total_seconds() / 86400
    level = (
        "CRITICAL"
        if days <= 0
        else "ERROR"
        if days < limits.certificate_error_days
        else "WARNING"
        if days < limits.certificate_warning_days
        else "OK"
    )
    return {
        "valid": True,
        "issuer": str(cert.get("issuer", ())),
        "expires_at": expires.isoformat(),
        "days_remaining": round(days, 2),
        "severity": level,
    }


def container_security(report: dict, containers: list[dict], state: dict) -> None:
    from booking_bot.deployment.templates import LOGGING

    expected = {"api", "worker", "postgres", "redis"}
    found = set()
    safe = rotation = restart = routes = True
    for container in containers:
        service = container["Config"]["Labels"].get("com.docker.compose.service")
        if service not in expected:
            continue
        found.add(service)
        host = container["HostConfig"]
        rotation &= host.get("LogConfig") == {
            "Type": LOGGING["driver"],
            "Config": LOGGING["options"],
        }
        restart &= host.get("RestartPolicy", {}).get("Name") == "unless-stopped"
        safe &= not host.get("Privileged") and host.get("NetworkMode") != "host"
        safe &= not host.get("CapAdd") and not any(
            "docker.sock" in mount.get("Source", "") for mount in container.get("Mounts", [])
        )
        if service in {"api", "worker"}:
            safe &= container["Config"].get("User", "").split(":")[0] not in {"", "root", "0"}
            safe &= host.get("ReadonlyRootfs", False)
            safe &= "ALL" in (host.get("CapDrop") or [])
            safe &= any(
                value.startswith("no-new-privileges") for value in host.get("SecurityOpt") or []
            )
        if service == "api" and state.get("public"):
            domain = state.get("domain", "")
            labels = container["Config"].get("Labels") or {}
            routes &= any(
                k.endswith(".rule") and v == f"Host(`{domain}`)" for k, v in labels.items()
            )
        if service == "worker":
            safe &= not any((host.get("PortBindings") or {}).values())
    complete = found == expected
    add(
        report,
        "container_privileges",
        "OK" if safe and complete else "CRITICAL",
        "Non-root apps, restricted privileges and no client Docker socket",
    )
    add(report, "log_rotation", "OK" if rotation and complete else "ERROR", "Docker logs: 10m x 3")
    add(
        report,
        "restart_policy",
        "OK" if restart and complete else "ERROR",
        "Services: unless-stopped",
    )
    if state.get("public"):
        add(
            report,
            "runtime_route",
            "OK" if routes and complete else "ERROR",
            "Actual Traefik hostname label",
        )


def client_report(
    manager: DeploymentManager,
    slug: str,
    backup_root: Path | None = None,
    *,
    production: bool = False,
    resources: bool = False,
    limits: Thresholds | None = None,
    telegram: bool = True,
) -> dict:
    from booking_bot.deployment.diagnostics import diagnose
    from booking_bot.deployment.manager import get_bot_identity, run_docker

    limits = limits or Thresholds.environment()
    report = {"slug": slug, "checks": [], "observed": True}
    try:
        state = manager.state(slug)
        path = manager.directory(slug)
        busy = lock_busy(path / ".lock") or lock_busy(manager.root / ".lock")
        report.update(
            deployment_state=state["status"],
            stage=state["stage"],
            version=state.get("current_version", "unknown"),
            domain=state.get("domain"),
            last_update=state.get("release_history", [])[-1:] or None,
        )
        if busy:
            report["observed"] = False
            add(
                report,
                "operation_lock",
                "WARNING",
                "Operation active; checks deferred, alerts retained",
            )
            return finish(report)
        add(report, "operation_lock", "OK", "No active operator lock")
        core = diagnose(manager, slug, resources=resources)
        for key in ("resources", "runtime", "image", "storage", "worker", "package_version"):
            if key in core:
                report[key] = core[key]
        for check in core["checks"]:
            level = (
                "OK"
                if check["ok"]
                else (
                    "CRITICAL"
                    if check["check"]
                    in {
                        "postgres",
                        "postgres_query",
                        "application_storage",
                        "permissions",
                        "network_isolation",
                    }
                    else "ERROR"
                )
            )
            add(report, check["check"], level, check["detail"])
        report["version"] = state.get("current_version", core.get("package_version", "unknown"))
        storage = core.get("storage", {})
        report["database_revision"] = storage.get("migrations", {}).get("current")
        report["database_size_bytes"] = storage.get("database_size_bytes")
        queue = storage.get("notifications")
        report["notifications"] = queue
        if queue is None:
            add(report, "notifications", "ERROR", "Queue counters unavailable; use a Phase 7 image")
        else:
            failed = queue["failed"]
            level = "ERROR" if failed >= limits.failed_jobs_error else "WARNING" if failed else "OK"
            add(report, "notifications", level, json.dumps(queue))
        ids = manager.compose(slug, "ps", "--all", "-q").split()
        containers = json.loads(run_docker(["inspect", *ids], timeout=20)) if ids else []
        container_security(report, containers, state)
        running = {row["service"] for row in core.get("runtime", []) if row["state"] == "running"}
        for service, mount in (("postgres", "/var/lib/postgresql/data"), ("redis", "/data")):
            if service not in running:
                continue
            try:
                output = manager.compose(slug, "exec", "-T", service, "df", "-P", mount, timeout=10)
                percent = float(output.strip().splitlines()[-1].split()[-2].rstrip("%"))
                add(
                    report,
                    f"{service}_volume_disk",
                    disk_level(percent, limits),
                    f"Volume filesystem {percent:.1f}% used; no recursive size scan",
                )
            except (DeploymentError, ValueError, IndexError):
                add(
                    report,
                    f"{service}_volume_disk",
                    "WARNING",
                    "Volume filesystem usage unavailable",
                )
        disk_check(report, "disk", path, limits)
        backup = backup_summary(manager, slug, backup_root)
        report["backup"] = backup
        age = backup["age_hours"]
        level = (
            "ERROR"
            if age is None or age >= limits.backup_error_hours
            else "WARNING"
            if age >= limits.backup_warning_hours
            else "OK"
        )
        add(report, "backup", level, json.dumps(backup))
        if backup["invalid"] or backup["incomplete"]:
            add(
                report,
                "backup_files",
                "WARNING",
                "Invalid/incomplete backup directories; inspect/prune explicitly",
            )
        from booking_bot.deployment.backup import BackupManager

        backups = BackupManager(manager, backup_root)
        disk_check(report, "backup_disk", backups.root, limits)
        if backups.root.exists():
            add(
                report,
                "backup_permissions",
                "OK"
                if private_permissions([backups.root, backups.directory(slug)])
                else "CRITICAL",
                "Private backup storage",
            )
            operation_path = backups.directory(slug) / "operations.json"
            if operation_path.exists():
                for name, event in read_json(operation_path).items():
                    add(
                        report,
                        f"{name}_operation",
                        "ERROR" if event["failed"] else "OK",
                        f"Last {name}: failed={event['failed']}; at={event['at']}",
                    )
        if production and backup["latest"]:
            try:
                backups.verify(slug, backup["latest"])
                add(
                    report,
                    "backup_integrity",
                    "OK",
                    "Latest checksum and pg_restore archive verified",
                )
            except DeploymentError:
                add(
                    report,
                    "backup_integrity",
                    "CRITICAL",
                    "Latest backup failed integrity verification",
                )
        if state.get("public"):
            try:
                cert = certificate(state["domain"], limits)
                report["tls"] = cert
                add(report, "tls_expiry", cert["severity"], json.dumps(cert))
            except (OSError, ValueError, KeyError):
                add(
                    report,
                    "tls_expiry",
                    "CRITICAL",
                    "Cannot validate HTTPS certificate/hostname/expiry",
                )
        elif production:
            add(
                report,
                "public_deployment",
                "ERROR",
                "Public HTTPS and Telegram webhook not configured",
            )
        # Core public doctor checks getMe. Internal test deployments use synthetic tokens.
        if telegram and not state.get("public"):
            try:
                bot = get_bot_identity(manager.values(slug)["TELEGRAM_BOT_TOKEN"])
                add(
                    report,
                    "telegram_bot",
                    "OK" if bot.id == state["bot_id"] else "ERROR",
                    "getMe identity",
                )
            except DeploymentError:
                add(
                    report, "telegram_bot", "ERROR", "Telegram unavailable or bot identity mismatch"
                )
        if report["version"] != core.get("image", {}).get("version"):
            add(
                report,
                "version_metadata",
                "ERROR",
                "Confirmed version differs from selected image label",
            )
    except (DeploymentError, OSError, ValueError, KeyError, TypeError):
        add(
            report,
            "diagnostic",
            "ERROR",
            "Diagnostic incomplete; inspect private deployment files/Docker",
        )
    return finish(report)


def host_report(manager: DeploymentManager, backup_root: Path | None = None) -> dict:
    from booking_bot.deployment import proxy
    from booking_bot.deployment.backup import BackupManager
    from booking_bot.deployment.manager import run_docker

    limits = Thresholds.environment()
    report = {"slug": "host", "checks": [], "observed": True}
    disk_check(report, "disk", manager.root, limits)
    backups = BackupManager(manager, backup_root)
    disk_check(report, "backup_disk", backups.root, limits)
    add(
        report, "backup_directory", "OK" if backups.root.is_dir() else "ERROR", "Backup root exists"
    )
    if backups.root.exists():
        add(
            report,
            "backup_permissions",
            "OK" if private_permissions([backups.root]) else "CRITICAL",
            "Private backup directory",
        )
    deployments = manager.list()
    report["deployments"] = len(deployments)
    token = os.environ.get("MONITORING_TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("MONITORING_TELEGRAM_CHAT_ID", "")
    configured = bool(
        re.fullmatch(r"\d{5,16}:[A-Za-z0-9_-]{20,}", token) and re.fullmatch(r"-?\d+", chat)
    )
    add(
        report,
        "alert_configuration",
        "OK" if configured else "ERROR" if token or chat else "WARNING",
        "Independent monitoring bot configured"
        if configured
        else "Monitoring alerts not configured correctly",
    )
    for file in (Path("/etc/booking-monitor.env"), Path("/etc/booking-backup.env")):
        if file.exists():
            add(
                report,
                file.name + "_permissions",
                "OK" if private_permissions([file]) else "CRITICAL",
                "Private host monitoring/backup credentials",
            )
    try:
        info = json.loads(run_docker(["info", "--format", "{{json .}}"], timeout=20))
        add(report, "docker", "OK" if info["OSType"] == "linux" else "ERROR", "Linux Docker daemon")
        docker_root = Path(info["DockerRootDir"])
        if os.name == "posix" and docker_root.is_dir():
            disk_check(report, "docker_disk", docker_root, limits)
        else:
            add(report, "docker_disk", "WARNING", "Docker VM storage not visible from this host")
        ids = run_docker(["ps", "-aq"], timeout=20).split()
        containers = json.loads(run_docker(["inspect", *ids], timeout=30)) if ids else []
        projects = {state.get("project") for state in deployments} | {"booking-proxy"}
        failed = [
            c["Name"].lstrip("/")
            for c in containers
            if (c["Config"].get("Labels") or {}).get("com.docker.compose.project") in projects
            and (
                c["Config"]["Labels"].get("com.docker.compose.service")
                in {"api", "worker", "postgres", "redis", "traefik", "socket-proxy"}
            )
            and (
                not c["State"]["Running"]
                or c["State"].get("Health", {}).get("Status") == "unhealthy"
            )
        ]
        report["failed_containers"] = failed
        add(
            report,
            "failed_containers",
            "ERROR" if failed else "OK",
            f"{len(failed)} failed Booking containers",
        )
        if lock_busy(manager.root / ".lock"):
            report["checks"][-1].update(
                severity="WARNING",
                ok=True,
                observed=False,
                detail="Container failures deferred during operator maintenance",
            )
        public = any(state.get("public") for state in deployments)
        initialized = (proxy.proxy_directory(manager.root) / "settings.json").exists()
        if public or initialized:
            add(
                report,
                "proxy",
                "OK" if proxy.status(manager.root) else "CRITICAL",
                "Shared Traefik",
            )
            network = json.loads(
                run_docker(["network", "inspect", proxy.PROXY_NETWORK], timeout=20)
            )[0]
            add(
                report,
                "proxy_network",
                "OK" if not network["Internal"] else "ERROR",
                "Public proxy network",
            )
            config = proxy.config(manager.root)
            add(
                report,
                "tls_infrastructure",
                "ERROR" if config["staging"] else "OK",
                "ACME production mode",
            )
            directory = proxy.proxy_directory(manager.root)
            add(
                report,
                "proxy_permissions",
                "OK" if private_permissions([directory, directory / ".env"]) else "CRITICAL",
                "Private proxy configuration",
            )
            proxied = next(
                (
                    c
                    for c in containers
                    if (c["Config"].get("Labels") or {}).get("com.docker.compose.project")
                    == "booking-proxy"
                    and c["Config"]["Labels"].get("com.docker.compose.service") == "socket-proxy"
                ),
                None,
            )
            add(
                report,
                "docker_socket_proxy",
                "OK"
                if proxied
                and proxied["State"]["Running"]
                and not any((proxied["HostConfig"].get("PortBindings") or {}).values())
                else "CRITICAL",
                "Restricted Docker API on private network",
            )
            safe_socket = bool(proxied)
            if proxied:
                env = set(proxied["Config"].get("Env") or [])
                safe_socket &= {"POST=0", "CONTAINERS=1", "NETWORKS=1", "EVENTS=1"} <= env
                safe_socket &= {
                    f"{name}=0"
                    for name in (
                        "AUTH",
                        "EXEC",
                        "SECRETS",
                        "BUILD",
                        "IMAGES",
                        "VOLUMES",
                        "ALLOW_START",
                        "ALLOW_STOP",
                        "ALLOW_RESTARTS",
                        "ALLOW_PAUSE",
                        "ALLOW_UNPAUSE",
                    )
                } <= env
                safe_socket &= set(proxied["NetworkSettings"]["Networks"]) == {
                    "booking-proxy_docker-api"
                }
                safe_socket &= not proxied["HostConfig"].get("Privileged")
            traefik = next(
                (
                    c
                    for c in containers
                    if (c["Config"].get("Labels") or {}).get("com.docker.compose.project")
                    == "booking-proxy"
                    and c["Config"]["Labels"].get("com.docker.compose.service") == "traefik"
                ),
                None,
            )
            safe_socket &= bool(traefik) and not any(
                "docker.sock" in mount.get("Source", "")
                for mount in (traefik or {}).get("Mounts", [])
            )
            add(
                report,
                "docker_api_restrictions",
                "OK" if safe_socket else "CRITICAL",
                "Read-only allowed API sections, private socket network, no direct Traefik socket",
            )
            if traefik:
                from booking_bot.deployment.templates import LOGGING

                rotation = all(
                    c["HostConfig"].get("LogConfig")
                    == {"Type": LOGGING["driver"], "Config": LOGGING["options"]}
                    for c in (traefik, proxied)
                    if c
                )
                add(
                    report,
                    "proxy_log_rotation",
                    "OK" if rotation else "ERROR",
                    "Proxy Docker logs bounded",
                )
                flags = traefik["Config"].get("Cmd") or []
                add(
                    report,
                    "proxy_dashboard",
                    "CRITICAL" if any("api.insecure=true" in f for f in flags) else "OK",
                    "No insecure Traefik dashboard",
                )
                mode = proxy.compose(
                    manager.root,
                    "exec",
                    "-T",
                    "traefik",
                    "stat",
                    "-c",
                    "%a",
                    "/letsencrypt/acme-production.json",
                ).strip()
                add(
                    report,
                    "acme_permissions",
                    "OK" if mode == "600" else "CRITICAL",
                    "ACME state mode 0600",
                )
        else:
            add(report, "proxy", "WARNING", "No proxy initialized; local deployments only")
        unsafe = []
        for c in containers:
            bindings = c["HostConfig"].get("PortBindings") or {}
            for port, rows in bindings.items():
                if port.split("/")[0] in {"5432", "6379", "8000", "2375", "2376"}:
                    if any(row.get("HostIp") not in {"127.0.0.1", "::1"} for row in rows or []):
                        unsafe.append(c["Name"].lstrip("/"))
        add(
            report,
            "public_ports",
            "CRITICAL" if unsafe else "OK",
            f"{len(unsafe)} public DB/Redis/API/Docker bindings; also audit host ss/firewall",
        )
    except (DeploymentError, ValueError, KeyError, TypeError):
        add(report, "infrastructure", "CRITICAL", "Docker/proxy infrastructure inspection failed")
    try:
        memory = Path("/proc/meminfo")
        if memory.exists():
            values = {
                line.split(":")[0]: int(line.split()[1]) for line in memory.read_text().splitlines()
            }
            used = 100 * (1 - values["MemAvailable"] / values["MemTotal"])
            report["memory_used_percent"] = round(used, 2)
            add(
                report,
                "memory",
                "ERROR"
                if used >= limits.memory_error
                else "WARNING"
                if used >= limits.memory_warning
                else "OK",
                f"RAM {used:.1f}% used",
            )
            load = os.getloadavg()[0] / (os.cpu_count() or 1)
            report["load_per_cpu"] = round(load, 2)
            add(
                report,
                "load",
                "WARNING" if load >= limits.load_warning else "OK",
                f"Load/CPU: {load:.2f}",
            )
        else:
            add(
                report, "host_resources", "WARNING", "Linux host memory/load unavailable on this OS"
            )
    except (OSError, ValueError, KeyError):
        add(report, "host_resources", "WARNING", "Cannot inspect Linux host resources")
    return finish(report)


def all_reports(
    manager: DeploymentManager, backup_root: Path | None = None, *, resources=False
) -> list[dict]:
    reports = [
        client_report(manager, row["slug"], backup_root, resources=resources)
        for row in manager.list()
    ]
    from booking_bot.version import __version__

    latest = os.environ.get("BOOKING_LATEST_VERSION", __version__)
    for report in reports:
        report["available_version"] = latest
        try:
            current = tuple(int(v) for v in report["version"].split("."))
            available = tuple(int(v) for v in latest.split("."))
            if len(current) != 3 or len(available) != 3:
                raise ValueError
            report["outdated"] = current < available
        except (ValueError, KeyError):
            report["outdated"] = None
        if report["outdated"]:
            add(
                report,
                "version_drift",
                "WARNING",
                f"OUTDATED: operator target {latest}; no automatic update",
            )
            finish(report)
    return reports


def table(reports: list[dict]) -> str:
    lines = [
        "CLIENT               VERSION      API       DB        REDIS     "
        "WORKER    WEBHOOK   BACKUP    STATE"
    ]
    for report in reports:
        checks = {c["check"]: c["severity"] for c in report["checks"]}
        values = [
            report["slug"],
            report.get("version", "unknown"),
            *(
                checks.get(k, "UNKNOWN")
                for k in (
                    "api_readiness",
                    "postgres_query",
                    "redis_ping",
                    "worker_heartbeat",
                    "telegram_webhook",
                    "backup",
                )
            ),
            report["severity"] + (" OUTDATED" if report.get("outdated") else ""),
        ]
        lines.append(f"{values[0]:20} {values[1]:12} " + " ".join(f"{v:9}" for v in values[2:]))
    return "\n".join(lines)

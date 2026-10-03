"""On-demand checks. Does not start stopped services or contact Telegram."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

from booking_bot.deployment.files import DeploymentError, private_permissions

if TYPE_CHECKING:
    from booking_bot.deployment.manager import DeploymentManager


def diagnose(manager: DeploymentManager, slug: str, *, resources: bool = True) -> dict:
    from booking_bot.deployment.manager import SERVICES, run_docker

    checks = []
    report = {"slug": slug, "ok": False, "checks": checks, "resources": []}

    def add(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    try:
        state = manager.state(slug)
    except DeploymentError as error:
        add("registry", False, str(error))
        return report
    add("registry", state["status"] == "READY", f"{state['status']}/{state['stage']}")
    path = manager.directory(slug)
    try:
        manager.local_config(slug)
        digest = manager.config_digest(slug)
        valid_config = state.get("config_sha256", digest) == digest
        add(
            "configuration",
            valid_config,
            "Validated" if valid_config else "File differs from applied config",
        )
    except Exception:
        valid_config = False
        add("configuration", False, "Invalid/missing TOML, .env, identity or pinned image")
    try:
        secret_paths = [path / ".env", path, manager.root]
        secret_paths += [
            p
            for name in ("creation.json", "configure-journal.json", "master-invite.txt")
            if (p := path / name).exists()
        ]
        add(
            "permissions",
            private_permissions(secret_paths),
            "Private registry and secret file access",
        )
    except (OSError, subprocess.TimeoutExpired):
        add("permissions", False, "Cannot inspect local file permissions")
    try:
        run_docker(["info", "--format", "{{.OSType}}"], timeout=20)
        add("docker", True, "Daemon available")
        rows = manager.runtime(slug)
        report["runtime"] = rows
    except DeploymentError:
        add(
            "docker",
            False,
            "Docker/Compose unavailable or incomplete files; inspect last-error.log",
        )
        return report
    running = {row["service"] for row in rows if row["state"] == "running"}
    for name in sorted(SERVICES):
        row = next((row for row in rows if row["service"] == name), None)
        add(
            name,
            row is not None and row["state"] == "running" and row["health"] == "healthy",
            f"{row['state']}/{row['health']}" if row else "Container missing",
        )

    for name, command in (
        (
            "api_live",
            (
                "api",
                "python",
                "-c",
                "import urllib.request; "
                "urllib.request.urlopen('http://localhost:8000/live',timeout=4)",
            ),
        ),
        (
            "postgres_query",
            ("postgres", "psql", "-U", "booking", "-d", "booking", "-Atqc", "SELECT 1"),
        ),
        (
            "redis_ping",
            ("redis", "sh", "-c", 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli ping | grep -qx PONG'),
        ),
        (
            "api_readiness",
            (
                "api",
                "python",
                "-c",
                "import urllib.request; "
                "urllib.request.urlopen('http://localhost:8000/ready',timeout=4)",
            ),
        ),
        ("worker_heartbeat", ("worker", "booking-admin", "worker-health")),
    ):
        if command[0] not in running:
            add(name, False, "Service stopped/missing; start or resume installation")
            continue
        try:
            output = manager.compose(slug, "exec", "-T", *command, timeout=30)
            add(name, True, output.strip() if name == "worker_heartbeat" else "Live check passed")
            if name == "worker_heartbeat":
                report["worker"] = {"heartbeat": output.strip()}
        except DeploymentError:
            add(name, False, "Live check failed; inspect service logs")
    # A helper is only run when storage is already running; --no-deps cannot start it.
    if valid_config and state.get("operation") != "configure" and {"postgres", "redis"} <= running:
        try:
            output = manager.compose(
                slug,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "admin",
                "python",
                "-m",
                "booking_bot.deployment.runtime",
                "doctor",
                timeout=60,
            )
            probe = json.loads(output)
            add(
                "application_storage",
                probe["postgres"] and probe["redis"],
                "DB/Redis checked using application credentials and network",
            )
            add("migrations", probe["migrations"]["ok"], json.dumps(probe["migrations"]))
            add("database_profile", probe["profile"], "DB specialist matches TOML")
            report["package_version"] = probe["package_version"]
            report["storage"] = probe
        except (DeploymentError, ValueError, KeyError):
            add(
                "migrations", False, "Runtime probe failed or image predates Phase 3B; inspect logs"
            )
    else:
        add("migrations", False, "Requires valid config and running storage; no services started")

    try:
        image = json.loads(run_docker(["image", "inspect", state["image_id"]]))[0]
        labels = image["Config"].get("Labels") or {}
        release = labels.get("org.opencontainers.image.version", "unknown")
        report["image"] = {
            "id": image["Id"],
            "reference": state["image_reference"],
            "version": release,
        }
        ids = manager.compose(slug, "ps", "--all", "-q").split()
        containers = json.loads(run_docker(["inspect", *ids])) if ids else []
        apps = [
            c
            for c in containers
            if c["Config"]["Labels"]["com.docker.compose.service"] in {"api", "worker"}
        ]
        add(
            "application_version",
            len(apps) == 2 and all(c["Image"] == state["image_id"] for c in apps),
            f"Release {release}; API/worker compared with pinned image ID",
        )
        data_network = state["project"] + "_data"
        network = json.loads(run_docker(["network", "inspect", data_network]))[0]
        isolated = network["Internal"]
        for container in containers:
            service = container["Config"]["Labels"]["com.docker.compose.service"]
            bindings = container["HostConfig"].get("PortBindings") or {}
            if service in {"postgres", "redis"}:
                isolated &= not any(bindings.values())
                isolated &= set(container["NetworkSettings"]["Networks"]) == {data_network}
            if service == "api":
                api_networks = set(container["NetworkSettings"]["Networks"])
                if state.get("public"):
                    isolated &= not any(bindings.values())
                    isolated &= "booking-proxy" in api_networks
                else:
                    isolated &= "booking-proxy" not in api_networks
                    isolated &= all(
                        b["HostIp"] == "127.0.0.1" for v in bindings.values() for b in (v or [])
                    )
            if service == "worker":
                isolated &= "booking-proxy" not in container["NetworkSettings"]["Networks"]
        add("network_isolation", isolated, "Private storage; only public API on proxy network")
        if "api" in running:
            mounted = manager.compose(
                slug,
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                "import hashlib;"
                "print(hashlib.sha256(open('/app/specialist.toml','rb').read()).hexdigest())",
            )
            add(
                "mounted_config",
                mounted.strip() == manager.config_digest(slug),
                "Running API bind mount matches installed file",
            )
        active = [c["Id"] for c in containers if c["State"]["Running"]]
        if active and resources:
            stats = run_docker(
                ["stats", "--no-stream", "--format", "{{json .}}", *active], timeout=30
            )
            report["resources"] = [
                {key: row[key] for key in ("Name", "MemUsage", "CPUPerc", "PIDs")}
                for line in stats.splitlines()
                if (row := json.loads(line))
            ]
            by_name = {c["Name"].lstrip("/"): c["Id"] for c in containers}
            for resource in report["resources"]:
                processes = run_docker(
                    ["top", by_name[resource["Name"]], "-eo", "pid,comm"], timeout=20
                )
                resource["Processes"] = max(0, len(processes.strip().splitlines()) - 1)
    except (DeploymentError, KeyError, TypeError, ValueError):
        add(
            "inspection",
            False,
            "Cannot inspect images/networks/resources; check Docker and missing containers",
        )
    if state.get("public"):
        from booking_bot.deployment import proxy
        from booking_bot.deployment.manager import get_bot_identity

        domain = state.get("domain", "")
        try:
            model = json.loads((path / "compose.yaml").read_text(encoding="utf-8"))
            labels = model["services"]["api"]["labels"]
            mapped = any(
                key.endswith(".rule") and value == f"Host(`{domain}`)"
                for key, value in labels.items()
            )
            add("domain_mapping", mapped, "Router maps this hostname")
        except (OSError, ValueError, KeyError, TypeError):
            add("domain_mapping", False, "Missing or invalid API route")
        try:
            dns = proxy.domain_check(manager.root, domain)
            add("dns", dns["ok"], json.dumps(dns))
        except DeploymentError:
            add("dns", False, "Domain or proxy IP configuration invalid")
        try:
            add("proxy", proxy.status(manager.root), "Shared Traefik is running")
            staging = proxy.config(manager.root)["staging"]
            result = proxy.https_check(domain, timeout_seconds=9, staging=staging)
            add("https", all(result.values()), f"/live={result['live']}; /ready={result['ready']}")
            if staging:
                add("certificate", False, "Staging ACME certificate is not trusted by Telegram")
            else:
                add("certificate", all(result.values()), "Trusted TLS for health endpoints")
        except DeploymentError:
            add("proxy", False, "Proxy configuration missing")
        try:
            identity = get_bot_identity(manager.values(slug)["TELEGRAM_BOT_TOKEN"])
            add("telegram_bot", identity.id == state["bot_id"], "getMe identity matches")
        except DeploymentError:
            add("telegram_bot", False, "Telegram getMe unavailable or identity mismatch")
        try:
            manager.compose(
                slug,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "admin",
                "booking-admin",
                "webhook-status",
                timeout=30,
            )
            add("telegram_webhook", True, "Telegram URL matches configured domain")
        except DeploymentError:
            add("telegram_webhook", False, "Webhook missing, mismatched or Telegram unavailable")
    report["ok"] = all(check["ok"] for check in checks)
    return report

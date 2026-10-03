"""Standalone Compose model: no source checkout or global .env dependency."""

import json
from copy import deepcopy
from dataclasses import asdict

from booking_bot.specialist_config import SpecialistTemplate

LOGGING = {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}


def legacy_compose_model(project: str, domain: str = "", *, public: bool = False) -> dict:
    """Exact Phase 3–6 template, accepted only for an explicit operational upgrade."""
    model = deepcopy(compose_model(project, domain, public=public))
    for name, service in model["services"].items():
        service.pop("logging", None)
        service.get("environment", {}).pop("LOG_SERVICE", None)
        if name in {"api", "worker"}:
            service["restart"] = "on-failure:5"
    return model


def render_specialist(template: SpecialistTemplate) -> str:
    lines = []
    for section, values in asdict(template).items():
        tables = values if section == "services" else [values]
        for table in tables:
            lines.append(f"[[{section}]]" if section == "services" else f"[{section}]")
            for key, value in table.items():
                if value is not None:
                    lines.append(f"{json.dumps(key)} = {json.dumps(value, ensure_ascii=False)}")
            lines.append("")
    return "\n".join(lines)


def compose_model(project: str, domain: str = "", *, public: bool = False) -> dict:
    environment = {
        "APP_ENV": "production",
        "TELEGRAM_WEBHOOK_MODE": "internal",
        "DATABASE_URL": "postgresql+asyncpg://booking:${POSTGRES_PASSWORD}@postgres:5432/booking",
        "REDIS_URL": "redis://:${REDIS_PASSWORD}@redis:6379/0",
        "TELEGRAM_BOT_TOKEN": "${TELEGRAM_BOT_TOKEN}",
        "TELEGRAM_WEBHOOK_HEADER_SECRET": "${TELEGRAM_WEBHOOK_HEADER_SECRET}",
        "SPECIALIST_CONFIG_PATH": "/app/specialist.toml",
        "LOG_SERVICE": "api",
    }
    app = {
        "image": "${BOOKING_IMAGE}",
        "pull_policy": "never",
        "environment": environment,
        "volumes": [
            {
                "type": "bind",
                "source": "./specialist.toml",
                "target": "/app/specialist.toml",
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        ],
        "read_only": True,
        "tmpfs": ["/tmp"],
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "networks": ["data", "egress"],
        "restart": "unless-stopped",
        "logging": LOGGING,
    }
    dependencies = {name: {"condition": "service_healthy"} for name in ("postgres", "redis")}
    api = {
        **app,
        "depends_on": dependencies,
        "command": ["python", "-m", "booking_bot.server"],
        "stop_grace_period": "90s",
        "healthcheck": {
            "test": [
                "CMD",
                "python",
                "-c",
                "import urllib.request; "
                "urllib.request.urlopen('http://localhost:8000/ready', timeout=4)",
            ],
            "interval": "5s",
            "timeout": "5s",
            "retries": 20,
            "start_period": "20s",
        },
    }
    if public:
        # The project includes a random suffix, so Docker provider names cannot collide.
        route = project.replace("-", "_")
        api["networks"] = ["data", "egress", "proxy"]
        api["expose"] = ["8000"]
        api["environment"] = {
            **environment,
            "TELEGRAM_WEBHOOK_MODE": "public",
            "TELEGRAM_WEBHOOK_BASE_URL": f"https://{domain}",
        }
        api["labels"] = {
            "traefik.enable": "true",
            "traefik.docker.network": "booking-proxy",
            f"traefik.http.routers.{route}.rule": f"Host(`{domain}`)",
            f"traefik.http.routers.{route}.entrypoints": "websecure",
            f"traefik.http.routers.{route}.tls.certresolver": "letsencrypt",
            f"traefik.http.routers.{route}.service": route,
            f"traefik.http.services.{route}.loadbalancer.server.port": "8000",
        }
    else:
        # Development/maintenance access remains bound to localhost only.
        api["ports"] = [{"target": 8000, "host_ip": "127.0.0.1", "published": "0"}]
    model = {
        "name": project,
        "services": {
            "admin": {
                **app,
                "environment": {**environment, "LOG_SERVICE": "admin"},
                "profiles": ["tools"],
                "restart": "no",
                "command": ["booking-admin", "configure"],
            },
            "api": api,
            "worker": {
                **app,
                "environment": {**environment, "LOG_SERVICE": "worker"},
                "depends_on": {**dependencies, "api": {"condition": "service_healthy"}},
                "command": ["booking-admin", "run-worker"],
                "stop_grace_period": "120s",
                "healthcheck": {
                    "test": ["CMD", "booking-admin", "worker-health"],
                    "interval": "10s",
                    "timeout": "15s",
                    "retries": 12,
                    "start_period": "30s",
                },
            },
            "postgres": {
                "image": "${POSTGRES_IMAGE}",
                "pull_policy": "never",
                "restart": "unless-stopped",
                "logging": LOGGING,
                "environment": {
                    "POSTGRES_USER": "booking",
                    "POSTGRES_DB": "booking",
                    "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}",
                },
                "volumes": ["postgres_data:/var/lib/postgresql/data"],
                "networks": ["data"],
                "stop_grace_period": "60s",
                "healthcheck": {
                    "test": ["CMD-SHELL", "pg_isready -U booking -d booking"],
                    "interval": "3s",
                    "timeout": "3s",
                    "retries": 30,
                },
            },
            "redis": {
                "image": "${REDIS_IMAGE}",
                "pull_policy": "never",
                "restart": "unless-stopped",
                "logging": LOGGING,
                "environment": {"REDIS_PASSWORD": "${REDIS_PASSWORD}"},
                "command": [
                    "sh",
                    "-c",
                    'exec redis-server --appendonly yes --requirepass "$$REDIS_PASSWORD"',
                ],
                "volumes": ["redis_data:/data"],
                "networks": ["data"],
                "stop_grace_period": "30s",
                "healthcheck": {
                    "test": [
                        "CMD-SHELL",
                        'REDISCLI_AUTH="$$REDIS_PASSWORD" redis-cli ping | grep -qx PONG',
                    ],
                    "interval": "3s",
                    "timeout": "3s",
                    "retries": 30,
                },
            },
        },
        "networks": {"data": {"internal": True}, "egress": {}},
        "volumes": {"postgres_data": {}, "redis_data": {}},
    }
    if public:
        model["networks"]["proxy"] = {"external": True, "name": "booking-proxy"}
        model["services"]["admin"]["environment"] = {**api["environment"], "LOG_SERVICE": "admin"}
    return model

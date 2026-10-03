"""Opt-in real Docker smoke; fake Telegram identity only, no real tokens or messages.

python tests/smoke_deployments.py --image booking-bot:VERSION --root tmp/phase3-smoke
Keeps both installations and volumes even on failure. Uses a NEW, dedicated registry.
"""

import argparse
import io
import json
import secrets
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from booking_bot.deployment.files import DeploymentError
from booking_bot.deployment.manager import CreateRequest, DeploymentManager, run_docker
from booking_bot.specialist_config import load_specialist_template


def smoke(root: Path, image: str) -> None:
    if root.exists():
        raise RuntimeError("Use a new smoke registry; previous test data is preserved")
    manager = DeploymentManager(root)
    template = load_specialist_template(Path(__file__).resolve().parents[1] / "specialist.toml")
    identities = []

    def getme(request, **kwargs):
        assert request.full_url.startswith("https://api.telegram.org/bot")
        assert request.full_url.endswith("/getMe")
        bot_id = int(request.full_url.split("/bot")[1].split(":")[0])
        identities.append(bot_id)
        return io.BytesIO(
            json.dumps(
                {
                    "ok": True,
                    "result": {
                        "id": bot_id,
                        "is_bot": True,
                        "username": f"smoke_{bot_id}_bot",
                    },
                }
            ).encode()
        )

    for slug, bot_id in (("smoke-alice", 911111111), ("smoke-bob", 922222222)):
        req = CreateRequest(
            replace(template, profile=replace(template.profile, slug=slug, brand_name=slug)),
            f"{bot_id}:{secrets.token_urlsafe(36)}",
            image,
        )
        with patch("booking_bot.deployment.manager.urllib.request.urlopen", getme):
            if slug == "smoke-alice":
                original = manager.compose

                def bad_migration(name, *args, _original=original, **kwargs):
                    if "alembic" in args:
                        args = (*args[:-1], "deliberately_missing_revision")
                    return _original(name, *args, **kwargs)

                with patch.object(manager, "compose", bad_migration):
                    try:
                        manager.create(slug, req)
                    except DeploymentError:
                        assert manager.state(slug)["stage"] == "migration"
                        assert manager.state(slug)["status"] == "FAILED"
                    else:
                        raise AssertionError("Expected real migration failure")
                before = manager.values(slug)
                assert manager.create(slug, resume=True)["status"] == "READY"
                assert before == manager.values(slug)
                print("Migration failure + recovery: OK", flush=True)
            else:
                assert manager.create(slug, req)["status"] == "READY"
        print(f"Created {slug}", flush=True)

    assert identities == [911111111, 922222222]
    first, second = "smoke-alice", "smoke-bob"
    assert manager.state(first)["project"] != manager.state(second)["project"]
    assert manager.values(first)["BOOKING_IMAGE"] == manager.values(second)["BOOKING_IMAGE"]
    endpoints = {}
    networks, volumes, containers = [], [], []
    storage_addresses = {}
    for slug in (first, second):
        runtime = manager.runtime(slug)
        assert len(runtime) == 4 and all(row["health"] == "healthy" for row in runtime)
        ids = manager.compose(slug, "ps", "-q").split()
        inspected = json.loads(run_docker(["inspect", *ids]))
        containers.append(set(ids))
        networks.append({n for row in inspected for n in row["NetworkSettings"]["Networks"]})
        volumes.append(
            {m["Name"] for row in inspected for m in row["Mounts"] if m["Type"] == "volume"}
        )
        for row in inspected:
            service = row["Config"]["Labels"]["com.docker.compose.service"]
            ports = row["NetworkSettings"]["Ports"] or {}
            if service in {"postgres", "redis", "worker"}:
                assert not any(ports.values())
            if service in {"postgres", "redis"}:
                storage_addresses[slug, service] = next(
                    iter(row["NetworkSettings"]["Networks"].values())
                )["IPAddress"]
            if service == "api":
                binding = ports["8000/tcp"][0]
                assert binding["HostIp"] == "127.0.0.1"
                endpoints[slug] = f"http://127.0.0.1:{binding['HostPort']}"
            if service in {"api", "worker"}:
                assert row["Image"] == manager.state(slug)["image_id"]
                assert row["Config"]["User"] == "10001:10001"
        assert urllib.request.urlopen(endpoints[slug] + "/ready", timeout=5).status == 200
        profile = manager.compose(
            slug,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "booking",
            "-Atc",
            "SELECT slug FROM businesses;",
        ).strip()
        assert profile == slug
        revision = manager.compose(
            slug,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "booking",
            "-Atc",
            "SELECT version_num FROM alembic_version;",
        ).strip()
        assert revision == "c75a01d29f10"
        invite_count = manager.compose(
            slug,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "booking",
            "-Atc",
            "SELECT count(*) FROM master_invites;",
        ).strip()
        assert invite_count == "1"
        values = manager.values(slug)
        public_output = json.dumps(manager.state(slug)) + manager.logs(slug, None, 100)
        assert all(
            values[key] not in public_output
            for key in (
                "TELEGRAM_BOT_TOKEN",
                "POSTGRES_PASSWORD",
                "REDIS_PASSWORD",
                "TELEGRAM_WEBHOOK_HEADER_SECRET",
            )
        )
    assert endpoints[first] != endpoints[second]
    for sets in (networks, volumes, containers):
        assert not sets[0] & sets[1]
    for source, target in ((first, second), (second, first)):
        for service, port in (("postgres", 5432), ("redis", 6379)):
            ip = storage_addresses[target, service]
            probe = (
                "import socket,sys\ntry:\n"
                f" socket.create_connection(({ip!r},{port}), timeout=2)\n"
                "except OSError:\n sys.exit(0)\nelse:\n sys.exit(1)"
            )
            manager.compose(source, "exec", "-T", "api", "python", "-c", probe)
    manager.compose(
        first,
        "exec",
        "-T",
        "redis",
        "sh",
        "-c",
        'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli SET smoke-isolation alice',
    )
    assert not manager.compose(
        second,
        "exec",
        "-T",
        "redis",
        "sh",
        "-c",
        'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli GET smoke-isolation',
    ).strip()
    before = {p.name: p.read_bytes() for p in manager.directory(first).iterdir()}
    manager.create(first)
    assert before == {p.name: p.read_bytes() for p in manager.directory(first).iterdir()}
    manager.action(first, "stop")
    assert all(row["state"] != "running" for row in manager.runtime(first))
    assert all(row["health"] == "healthy" for row in manager.runtime(second))
    assert urllib.request.urlopen(endpoints[second] + "/ready", timeout=5).status == 200
    manager.action(first, "start")
    manager.action(first, "restart")
    assert all(row["health"] == "healthy" for row in manager.runtime(first))
    assert (
        manager.compose(
            first,
            "exec",
            "-T",
            "redis",
            "sh",
            "-c",
            'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli GET smoke-isolation',
        ).strip()
        == "alice"
    )
    print(
        "PASS: two healthy deployments, migration recovery, immutable image, separate DB/Redis, "
        "volumes/networks/processes, loopback API, invite, redaction, idempotency, "
        "stop/start/restart"
    )
    print(f"Preserved test registry: {root.absolute()}")
    from deployment_lifecycle_smoke import lifecycle
    lifecycle(manager, first, second)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    smoke(args.root, args.image)

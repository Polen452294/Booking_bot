"""Focused real new-client/restore acceptance for Redis's privilege-drop fix."""

import argparse
import ipaddress
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from booking_bot.deployment.backup import BackupManager
from booking_bot.deployment.manager import BotIdentity, CreateRequest, DeploymentManager, run_docker
from booking_bot.specialist_config import load_specialist_template
from disaster_recovery_smoke import SEED, snapshot


def smoke(root, image, network_pool=None):
    if root.exists():
        raise RuntimeError("Use a new dedicated Redis qualification registry")
    manager = DeploymentManager(root)
    if network_pool:
        pool = ipaddress.ip_network(network_pool)
        if pool.version != 4 or pool.prefixlen != 22 or not pool.is_private:
            raise ValueError("Test network pool must be a private IPv4 /22")
        subnets = iter(pool.subnets(new_prefix=24))
        provision = manager.provision

        def test_provision(slug, state):
            for name in ("data", "egress"):
                run_docker(
                    [
                        "network",
                        "create",
                        "--subnet",
                        str(next(subnets)),
                        "--label",
                        f"com.docker.compose.project={state['project']}",
                        "--label",
                        f"com.docker.compose.network={name}",
                        *(["--internal"] if name == "data" else []),
                        f"{state['project']}_{name}",
                    ]
                )
            return provision(slug, state)

        manager.provision = test_provision
    backups = BackupManager(manager, root.with_name(root.name + "-backups"))
    template = load_specialist_template(Path(__file__).resolve().parents[1] / "specialist.toml")
    slugs = ("release-test-1", "release-test-2")
    baseline = {}
    for index, slug in enumerate(slugs):
        token = f"95555555{index}:" + "synthetic_" * 4
        request = CreateRequest(
            replace(template, profile=replace(template.profile, slug=slug)), token, image
        )
        with patch(
            "booking_bot.deployment.manager.get_bot_identity",
            return_value=BotIdentity(955555550 + index, f"redis_rc_{index}_bot"),
        ):
            manager.create(slug, request)
        assert manager.doctor(slug)["ok"]
        manager.compose(slug, "run", "--rm", "--no-deps", "-T", "admin", "python", "-c", SEED)
        baseline[slug] = snapshot(backups, slug)
        pid = manager.compose(
            slug, "exec", "-T", "redis", "sh", "-c", 'grep -E "^Uid:|^CapEff:" /proc/1/status'
        )
        assert "Uid:\t999\t999\t999\t999" in pid, pid
        assert "CapEff:\t0000000000000000" in pid, pid
    a, b = slugs
    peer = manager.compose(b, "ps", "-q")
    manager.compose(
        a,
        "exec",
        "-T",
        "redis",
        "sh",
        "-c",
        'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli set qualification retained',
    )
    for args in (("restart", "redis"), ("up", "-d", "--force-recreate", "--wait", "redis")):
        manager.compose(a, *args)
        assert (
            manager.compose(
                a,
                "exec",
                "-T",
                "redis",
                "sh",
                "-c",
                'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli get qualification',
            ).strip()
            == "retained"
        )
        pid = manager.compose(a, "exec", "-T", "redis", "sh", "-c", 'grep "^Uid:" /proc/1/status')
        assert "999\t999\t999\t999" in pid, pid
        assert snapshot(backups, a) == baseline[a]
        assert manager.compose(b, "ps", "-q") == peer
    backup = backups.create(a)
    backups.verify(a, backup)
    backups.sql(a, "UPDATE businesses SET name='post backup synthetic change'")
    assert snapshot(backups, a) != baseline[a]
    backups.restore(a, backup, yes=True)
    assert snapshot(backups, a) == baseline[a]
    assert snapshot(backups, b) == baseline[b]
    assert manager.compose(b, "ps", "-q") == peer
    assert all(manager.doctor(slug)["ok"] for slug in slugs)
    (root.parent / "redis-runtime-result.json").write_text(
        json.dumps(
            {
                "uid": 999,
                "effective_capabilities": 0,
                "restart_recreate_persistence": "passed",
                "backup_restore": "passed",
                "two_client_isolation": "passed",
                "real_telegram": "not tested",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        "REDIS RUNTIME PASSED: UID 999/caps 0, new clients, restart/recreate, restore, peer",
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--network-pool")
    args = parser.parse_args()
    smoke(args.root.absolute(), args.image, args.network_pool)

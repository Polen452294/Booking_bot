"""Opt-in destructive tests ONLY on a new dedicated registry with synthetic Telegram tokens.

Runs real two-client DR first, then restart/outage/Redis-loss/load/alert acceptance.
Never reboots the operator host, uses no real Telegram messaging, preserves test artifacts.
"""

import argparse
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from booking_bot.deployment.alerts import reconcile
from booking_bot.deployment.backup import BackupManager
from booking_bot.deployment.files import DeploymentError, atomic_write, read_json
from booking_bot.deployment.manager import DeploymentManager, run_docker
from booking_bot.deployment.monitoring import Thresholds, client_report
from disaster_recovery_smoke import smoke as disaster_recovery
from disaster_recovery_smoke import snapshot


def url(manager, slug):
    row = next(row for row in manager.runtime(slug) if row["service"] == "api")
    port = next(p["PublishedPort"] for p in row["ports"] if p["TargetPort"] == 8000)
    return f"http://127.0.0.1:{port}"


def request(origin, route="/ready", payload=None, secret=None):
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret
    req = urllib.request.Request(
        origin + route, data=json.dumps(payload).encode() if payload else None, headers=headers
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            status = response.status
            response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
    return status, time.perf_counter() - start


def wait_ready(manager, slug, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if request(url(manager, slug))[0] == 200 and manager.doctor(slug)["ok"]:
                return
        except (DeploymentError, OSError):
            pass
        time.sleep(3)
    raise AssertionError(f"{slug} did not recover within {timeout}s")


def smoke(root, image, network_pool, *, resume=False, load_only=False):
    if load_only and not resume:
        raise RuntimeError("Load continuation requires an owned --resume test registry")
    marker = root.parent / "operations-baseline.json"
    if not resume:
        disaster_recovery(root, image, network_pool, test_domains=True)
    manager = DeploymentManager(root)
    backups = BackupManager(manager, root.with_name(root.name + "-backups"))
    a, b = "dr-test", "dr-test-b"
    identities = {slug: manager.state(slug)["project"] for slug in (a, b)}
    if resume:
        saved = read_json(marker)
        if saved["projects"] != identities or saved["root"] != str(root):
            raise RuntimeError("Resume only the owned operations test registry")
        before = saved["before"]
    else:
        before = {slug: snapshot(backups, slug) for slug in (a, b)}
        atomic_write(
            marker, json.dumps({"projects": identities, "root": str(root), "before": before})
        )
    peer_ids = manager.compose(b, "ps", "-q")
    results = {"image": image, "disaster_recovery": "passed", "host_reboot": "not executed"}
    messages = []

    def sender(message):
        messages.append(message)
        return True

    def report(limits=None):
        return client_report(manager, a, backups.root, limits=limits, telegram=False)

    def check_alert():
        result = report()
        assert not result["ok"], result
        assert (
            reconcile(root.parent / (root.name + "-alerts"), [result], sender=sender)["sent"] == 1
        )
        assert (
            reconcile(root.parent / (root.name + "-alerts"), [result], sender=sender)["sent"] == 0
        )
        return result

    def check_recovery():
        wait_ready(manager, a)
        result = report()
        assert result["ok"], result
        assert (
            reconcile(root.parent / (root.name + "-alerts"), [result], sender=sender)["sent"] == 1
        )
        assert "RECOVERED" in messages[-1]

    if not load_only:
        baseline = report()
        assert baseline["ok"], baseline
        for service in ("api", "worker", "redis", "postgres"):
            manager.compose(a, "restart", service)
            wait_ready(manager, a)
            assert snapshot(backups, a) == before[a]
            assert manager.compose(b, "ps", "-q") == peer_ids
            print(f"RESTART PASSED: {service}, data and peer preserved", flush=True)
        results["service_restarts"] = "passed"

        # Unexpected exit, not Compose stop. Docker must restart the worker automatically.
        worker_id = manager.compose(a, "ps", "-q", "worker").strip()
        worker = json.loads(run_docker(["inspect", worker_id]))[0]
        assert worker["Config"]["Labels"]["com.docker.compose.project"] == identities[a]
        count_before = worker["RestartCount"]
        # PID 1 ignores same-namespace signals. A narrowly scoped host-PID helper simulates
        # an external process crash, checking both cgroup identity and command before SIGKILL.
        crash = (
            "import os,sys,pathlib; pid=int(sys.argv[1]); cid=sys.argv[2]; assert pid>1; "
            "assert cid in pathlib.Path(f'/proc/{pid}/cgroup').read_text(); "
            "assert b'run-worker' in pathlib.Path(f'/proc/{pid}/cmdline').read_bytes(); "
            "os.kill(pid,9)"
        )
        run_docker(
            [
                "run",
                "--rm",
                "--pid=host",
                "--cgroupns=host",
                "--user",
                "0",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "KILL",
                "--entrypoint",
                "python",
                image,
                "-c",
                crash,
                str(worker["State"]["Pid"]),
                worker_id,
            ]
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if json.loads(run_docker(["inspect", worker_id]))[0]["RestartCount"] > count_before:
                break
            time.sleep(2)
        else:
            raise AssertionError("Worker unexpected kill was not automatically restarted")
        wait_ready(manager, a)
        results["worker_kill"] = "passed"
        print("WORKER KILL PASSED: automatic restart", flush=True)

        for service, critical in (
            ("postgres", True),
            ("redis", False),
            ("worker", False),
            ("api", False),
        ):
            manager.compose(a, "stop", service)
            if service in {"postgres", "redis"}:
                assert request(url(manager, a))[0] == 503
            failed = check_alert()
            if critical:
                assert failed["severity"] == "CRITICAL"
            assert request(url(manager, b))[0] == 200
            manager.compose(a, "up", "-d", "--wait", "--wait-timeout", "180", service)
            check_recovery()
            print(f"OUTAGE PASSED: {service}, failure/dedup/recovery alerts", flush=True)
        results["outages_alerts"] = "passed (capturing sender; no live Telegram)"

        manager.compose(
            a,
            "exec",
            "-T",
            "redis",
            "sh",
            "-c",
            'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli SET phase7-test-hold temporary',
        )
        manager.compose(
            a,
            "exec",
            "-T",
            "redis",
            "sh",
            "-c",
            'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli FLUSHALL',
        )
        assert snapshot(backups, a) == before[a]
        assert (
            manager.compose(
                a,
                "exec",
                "-T",
                "redis",
                "sh",
                "-c",
                'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli EXISTS phase7-test-hold',
            ).strip()
            == "0"
        )
        wait_ready(manager, a)
        results["redis_loss"] = (
            "passed: PostgreSQL intact, temporary key lost, heartbeat regenerated"
        )
        print("REDIS LOSS PASSED: durable PostgreSQL data preserved", flush=True)

        # Deterministic write denial, confined to the backup transfer of this test client.
        with patch.object(backups, "transfer", side_effect=DeploymentError("test write denied")):
            try:
                backups.create(a)
            except DeploymentError:
                pass
            else:
                raise AssertionError("Backup write failure accepted")
        failed = check_alert()
        assert any(
            c["check"] == "backup_operation" and c["severity"] == "ERROR" for c in failed["checks"]
        )
        backups.create(a)
        check_recovery()
        results["backup_failure"] = (
            "passed: injected write denial, durable failure latch and recovery"
        )
        assert (
            report(Thresholds(disk_warning=0.001, disk_error=0.002, disk_critical=0.003))[
                "severity"
            ]
            == "CRITICAL"
        )
        results["disk_full"] = "passed with mock thresholds; disk not filled"

        atomic_write(root.parent / "operations-preload.json", json.dumps(results))
    elif (root.parent / "operations-preload.json").exists():
        results.update(read_json(root.parent / "operations-preload.json"))
    else:
        results["previous_operations"] = (
            "Prior completed scenarios are recorded in preserved operations log"
        )

    origin = url(manager, a)
    secret = manager.values(a)["TELEGRAM_WEBHOOK_HEADER_SECRET"]
    from booking_bot.deployment.proxy import webhook_url

    for key in (
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_WEBHOOK_HEADER_SECRET",
    ):
        assert manager.values(a)[key] != manager.values(b)[key]
    if manager.state(a)["domain"]:
        assert webhook_url(manager.state(a)["domain"]) != webhook_url(manager.state(b)["domain"])
    states = {slug: manager.state(slug) for slug in (a, b)}
    postgres_ips = {}
    volume_names = []
    for slug in (a, b):
        pg_id = manager.compose(slug, "ps", "-q", "postgres").strip()
        container = json.loads(run_docker(["inspect", pg_id]))[0]
        postgres_ips[slug] = container["NetworkSettings"]["Networks"][
            states[slug]["project"] + "_data"
        ]["IPAddress"]
        volume_names.append(next(m["Name"] for m in container["Mounts"] if m["Type"] == "volume"))
    assert len(set(volume_names)) == 2
    isolation = (
        "import socket,sys; assert socket.gethostbyname('postgres')==sys.argv[1]; "
        "\ntry: socket.create_connection((sys.argv[2],5432),timeout=2)"
        "\nexcept OSError: pass\nelse: raise AssertionError('Foreign database reachable')"
    )
    manager.compose(
        a, "exec", "-T", "api", "python", "-c", isolation, postgres_ips[a], postgres_ips[b]
    )
    manager.compose(
        a, "exec", "-T", "worker", "python", "-c", isolation, postgres_ips[a], postgres_ips[b]
    )
    results["negative_isolation"] = (
        "passed: A API/worker cannot connect to B DB IP; own DNS resolves only own DB"
    )

    def load(index):
        if index % 3 == 0:
            return request(origin)
        if index % 3 == 1:
            return request(origin, "/live")
        # Valid ignored Telegram Poll updates traverse auth/Redis/DB receipt/FSM pipeline.
        # No poll handlers exist, so these cannot send Telegram messages.
        payload = {
            "update_id": 700000 + index,
            "poll": {
                "id": str(index),
                "question": "synthetic",
                "options": [{"persistent_id": str(index), "text": "a", "voter_count": 0}],
                "total_voter_count": 0,
                "is_closed": True,
                "is_anonymous": True,
                "type": "regular",
                "allows_multiple_answers": False,
                "allows_revoting": False,
                "members_only": False,
            },
        }
        return request(origin, "/api/v1/webhooks/telegram", payload, secret)

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=32) as pool:
        measurements = list(pool.map(load, range(96)))
    elapsed = time.perf_counter() - start
    assert all(status == 200 for status, _ in measurements), measurements
    latencies = sorted(duration for _, duration in measurements)
    results["performance"] = {
        "concurrency": 32,
        "requests": 96,
        "all_status": 200,
        "seconds": round(elapsed, 3),
        "p95_seconds": round(latencies[91], 3),
        "max_seconds": round(latencies[-1], 3),
    }
    for slug in (a, b):
        wait_ready(manager, slug)
        assert snapshot(backups, slug) == before[slug]
    resources = []
    for slug in (a, b):
        ids = manager.compose(slug, "ps", "-q").split()
        stats = run_docker(["stats", "--no-stream", "--format", "{{json .}}", *ids])
        resources.append({"slug": slug, "stats": [json.loads(line) for line in stats.splitlines()]})
    info = json.loads(run_docker(["info", "--format", "{{json .}}"], timeout=20))
    results["test_environment"] = {
        key: info.get(key) for key in ("NCPU", "MemTotal", "OperatingSystem", "Architecture")
    }
    results["resources"] = resources
    results["cross_client_isolation"] = "passed: data/peer container IDs throughout outages and DR"
    results["production_check"] = "intentionally blocked: no public domain/TLS/live Telegram"
    results["alert_messages_captured"] = len(messages)
    atomic_write(root.parent / (root.name + "-results.json"), json.dumps(results, indent=2))
    print(
        "OPERATIONS ACCEPTANCE PASSED: restarts, kill, outages, alerts, "
        "Redis, backup, load, isolation",
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--network-pool")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--load-only", action="store_true")
    args = parser.parse_args()
    smoke(
        args.root.absolute(),
        args.image,
        args.network_pool,
        resume=args.resume,
        load_only=args.load_only,
    )

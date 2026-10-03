import json
from unittest.mock import Mock

import pytest

from booking_bot.deployment import releases as module
from booking_bot.deployment.cli import build_parser
from booking_bot.deployment.files import DeploymentError, registry_lock
from booking_bot.deployment.releases import ReleaseManager, inspect_image, target_reference
from booking_bot.version import (
    __version__,
    parse_current_version,
    parse_release_version,
    parse_version,
    version_order,
)
from test_bookingctl import manager, request  # noqa: F401

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64
REVISION = "b62f3d910ea4"
BACKUP = "20261001T000000Z-aabbccdd"


@pytest.mark.parametrize("value", ["latest", "v1.2.3", "1.2", "01.2.3", "1.2.3\n", "1.2.3-rc1"])
def test_invalid_versions(value):
    with pytest.raises(ValueError):
        parse_version(value)


def test_version_and_cli():
    assert parse_release_version(__version__) == (1, 0, 0)
    assert target_reference("1.2.3") == "ghcr.io/polen452294/booking-bot:1.2.3"
    args = build_parser().parse_args(["update", "alice", "--version", "1.2.3", "--dry-run"])
    assert args.version == "1.2.3" and args.dry_run
    assert build_parser().parse_args(["version"]).slug is None


@pytest.mark.parametrize(
    "value",
    [
        "1.0.0-rc.0",
        "1.0.0-rc.01",
        "1.0.0-rc1",
        "1.0.0-beta.1",
        "1.0.0+build",
        "01.0.0-rc.1",
        "latest",
        "v1.0.0-rc.1",
        "1.0.0-rc.1\n",
    ],
)
def test_invalid_release_targets(value):
    with pytest.raises(ValueError):
        parse_release_version(value)


def test_rc_targets_and_semver_precedence():
    assert target_reference("1.0.0-rc.1").endswith(":1.0.0-rc.1")
    versions = ["0.7.0", "1.0.0-rc.1", "1.0.0-rc.2", "1.0.0-rc.10", "1.0.0"]
    assert sorted(reversed(versions), key=version_order) == versions
    assert version_order("1.0.0+build") == version_order("1.0.0")


@pytest.mark.parametrize(
    "current,target,allowed",
    [
        ("1.0.0-rc.1", "1.0.0-rc.2", True),
        ("1.0.0-rc.2", "1.0.0-rc.1", False),
        ("1.0.0-rc.1", "1.0.0-rc.1", False),
        ("1.0.0", "1.0.0-rc.2", False),
        ("1.0.0-rc.10", "1.0.0", True),
    ],
)
def test_rc_update_preflight_order(releases, monkeypatch, current, target, allowed):
    state = releases.manager.state("alice")
    state["current_version"] = current
    releases.manager.save("alice", state, "READY", "complete")
    monkeypatch.setattr(module, "inspect_image", lambda _: {"id": OLD, "version": current})
    if allowed:
        releases.preflight("alice", target)
    else:
        with pytest.raises(DeploymentError, match="newer version"):
            releases.preflight("alice", target)


def test_current_phase_image_is_semver_and_can_upgrade(releases, monkeypatch):
    docker = module.run_docker

    def legacy(args, **kw):
        output = docker(args, **kw)
        if args[:3] == ["image", "inspect", OLD]:
            item = json.loads(output)
            item[0]["Config"]["Labels"]["org.opencontainers.image.version"] = "0.5.0-phase5"
            return json.dumps(item)
        return output

    state = releases.manager.state("alice")
    state["current_version"] = "0.5.0-phase5"
    releases.manager.save("alice", state, "READY", "complete")
    monkeypatch.setattr(module, "run_docker", legacy)
    assert parse_current_version("0.5.0-phase5") == ((0, 5, 0), True)
    releases.update("alice", "0.6.0")
    assert releases.manager.state("alice")["previous_version"] == "0.5.0-phase5"
    assert releases.rollback("alice")["status"] == "OK"


@pytest.mark.parametrize("value", ["01.0.0-phase5", "1.0.0-01", "1.0.0-", "unknown"])
def test_invalid_current_version_is_rejected(value):
    with pytest.raises(ValueError):
        parse_current_version(value)


@pytest.fixture
def releases(manager, monkeypatch):  # noqa: F811
    manager.create("alice", request())
    state = manager.state("alice")
    state["current_version"] = "0.5.0"
    manager.save("alice", state, "READY", "complete")
    instance = ReleaseManager(manager, manager.root.parent / "backups")
    calls = []
    instance.calls = calls

    def docker(args, **kw):
        calls.append(tuple(args))
        if args[:2] == ["image", "inspect"]:
            old = args[2] == OLD
            return json.dumps(
                [
                    {
                        "Id": OLD if old else NEW,
                        "Config": {
                            "Labels": {
                                "org.opencontainers.image.version": "0.5.0" if old else "0.6.0",
                                "org.opencontainers.image.revision": "c" * 40,
                                "org.opencontainers.image.source": "https://github.com/Polen452294/Booking_bot",
                            }
                        },
                    }
                ]
            )
        return str(manager.root) if args[0] == "info" else "{}"

    monkeypatch.setattr(module, "run_docker", docker)
    monkeypatch.setattr(module.shutil, "which", lambda name: "docker")
    monkeypatch.setattr(module.shutil, "disk_usage", lambda path: Mock(free=10 * 1024**3))
    monkeypatch.setattr(
        instance.backups,
        "sql",
        lambda slug, query, *a: "10000" if "pg_database_size" in query else REVISION,
    )
    monkeypatch.setattr(instance.backups, "managed_config", lambda slug: None)
    monkeypatch.setattr(
        instance.backups,
        "_create",
        Mock(side_effect=lambda *a, **kw: calls.append(("backup",)) or BACKUP),
    )
    monkeypatch.setattr(instance.backups, "verify", Mock(return_value={"image_id": OLD}))

    def compose(slug, *args, **kwargs):
        calls.append((slug, *args))
        if "create-master-invite" in args:
            return "https://t.me/test_booking_bot?start=master_testinvite"
        if "release-check" in args:
            return json.dumps(
                {"heads": [REVISION], "current": [REVISION], "forward": True, "version": "0.6.0"}
            )
        return ""

    monkeypatch.setattr(manager, "compose", compose)
    monkeypatch.setattr(
        manager,
        "doctor",
        lambda slug: {
            "ok": manager.state(slug)["status"] == "READY",
            "checks": [
                {"check": "registry", "ok": manager.state(slug)["status"] == "READY"},
                {"check": "application_storage", "ok": True},
            ],
        },
    )
    return instance


def test_success_backup_order_confirmed_metadata_and_history(releases, monkeypatch):
    original = releases.validate

    def validate(slug):
        assert releases.manager.state(slug)["current_version"] == "0.5.0"
        assert releases.manager.state(slug)["status"] == "UPDATING"
        original(slug)

    monkeypatch.setattr(releases, "validate", validate)
    event = releases.update("alice", "0.6.0")
    state = releases.manager.state("alice")
    assert state["current_version"] == "0.6.0"
    assert state["previous_version"] == "0.5.0" and state["backup_id"] == BACKUP
    assert state["status"] == "READY" and "operation" not in state
    assert state["image_id"] == NEW
    assert releases.manager.values("alice")["BOOKING_IMAGE"] == NEW
    assert event["status"] == "OK" and releases.history("alice") == [event]
    backup_pos = releases.calls.index(("backup",))
    pull_pos = next(i for i, c in enumerate(releases.calls) if c[0] == "pull")
    migration_pos = next(i for i, c in enumerate(releases.calls) if "upgrade" in c)
    assert backup_pos < pull_pos < migration_pos


def test_dry_run_preserves_every_file(releases):
    root = releases.manager.root
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
    plan = releases.update("alice", "0.6.0", dry_run=True)
    after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
    assert before == after and plan["dry_run"]
    assert plan["migration_state"]["current"] == REVISION
    assert not any(c[0] in {"backup", "pull"} or "upgrade" in c for c in releases.calls)


def test_unavailable_image_never_backs_up_or_changes_state(releases, monkeypatch):
    before = releases.manager.state("alice")
    docker = module.run_docker

    def unavailable(args, **kw):
        if args[0] == "manifest":
            raise DeploymentError("No such manifest")
        return docker(args, **kw)

    monkeypatch.setattr(module, "run_docker", unavailable)
    with pytest.raises(DeploymentError):
        releases.update("alice", "0.6.0")
    assert releases.manager.state("alice") == before
    releases.backups._create.assert_not_called()


@pytest.mark.parametrize("target", ["0.5.0", "0.4.0", "latest"])
def test_not_a_forward_release(releases, target):
    with pytest.raises(DeploymentError):
        releases.update("alice", target)
    releases.backups._create.assert_not_called()


@pytest.mark.parametrize("failure", ["backup", "migration", "health", "doctor"])
def test_failure_is_persisted_and_current_version_unchanged(releases, monkeypatch, failure):
    if failure == "backup":
        monkeypatch.setattr(releases.backups, "_create", Mock(side_effect=DeploymentError("dump")))
    elif failure == "doctor":
        monkeypatch.setattr(
            releases.manager,
            "doctor",
            Mock(side_effect=[{"ok": True}, {"checks": [{"check": "worker", "ok": False}]}]),
        )
    else:
        original = releases.manager.compose

        def compose(slug, *args, **kwargs):
            if (failure == "migration" and "upgrade" in args) or (
                failure == "health" and args[0] == "up"
            ):
                raise DeploymentError("test failure")
            return original(slug, *args, **kwargs)

        monkeypatch.setattr(releases.manager, "compose", compose)
    with pytest.raises(DeploymentError, match="Update FAILED") as caught:
        releases.update("alice", "0.6.0")
    state = releases.manager.state("alice")
    assert state["status"] == "FAILED" and state["current_version"] == "0.5.0"
    assert state["release_history"][-1]["status"] == "FAILED"
    assert "recovery=" in str(caught.value)
    if failure == "backup":
        assert not any(c[0] == "pull" for c in releases.calls)
    else:
        assert state["release_attempt"]["backup_id"] == BACKUP


@pytest.mark.parametrize("heads,forward", [([], True), (["one", "two"], True), (["one"], False)])
def test_invalid_migration_graph_stops_before_services(releases, monkeypatch, heads, forward):
    monkeypatch.setattr(
        releases.manager,
        "compose",
        lambda *a, **kw: json.dumps(
            {"heads": heads, "current": [REVISION], "forward": forward, "version": "0.6.0"}
        ),
    )
    with pytest.raises(DeploymentError, match="Update FAILED"):
        releases.update("alice", "0.6.0")
    assert releases.manager.state("alice")["image_id"] == OLD


def test_application_rollback_never_runs_downgrade_or_migration(releases):
    releases.update("alice", "0.6.0")
    releases.calls.clear()
    event = releases.rollback("alice")
    assert event["status"] == "OK"
    state = releases.manager.state("alice")
    assert state["image_id"] == OLD and state["current_version"] == "0.5.0"
    assert len(releases.history("alice")) == 2
    assert not any("alembic" in c for c in releases.calls)


def test_schema_change_requires_explicit_restore(releases, monkeypatch):
    releases.update("alice", "0.6.0")
    monkeypatch.setattr(releases.backups, "sql", lambda *a: "changed_revision")
    with pytest.raises(DeploymentError, match="Database restore required"):
        releases.rollback("alice")
    with pytest.raises(DeploymentError, match="--yes"):
        releases.rollback("alice", restore_database=True)
    restore = Mock()
    monkeypatch.setattr(releases.backups, "_restore", restore)
    releases.rollback("alice", restore_database=True, yes=True)
    assert restore.call_args.kwargs["safety_backup"] == BACKUP


def test_common_backup_restore_update_lock(releases):
    with registry_lock(releases.manager.root):
        for operation in (
            lambda: releases.update("alice", "0.6.0"),
            lambda: releases.backups.create("alice"),
            lambda: releases.backups.restore("alice", BACKUP, yes=True),
            lambda: releases.update_all("0.6.0"),
        ):
            with pytest.raises(DeploymentError, match="busy"):
                operation()
    assert releases.manager.state("alice")["status"] == "READY"


def test_rollout_stops_on_first_failed_client(releases, monkeypatch):
    monkeypatch.setattr(releases.manager, "list", lambda: [{"slug": "alice"}, {"slug": "bob"}])
    update = Mock(side_effect=DeploymentError("client A failed"))
    monkeypatch.setattr(releases, "_update", update)
    with pytest.raises(DeploymentError):
        releases.update_all("0.6.0")
    assert update.call_count == 1 and update.call_args.args[0] == "alice"


def test_target_metadata_mismatch(releases, monkeypatch):
    monkeypatch.setattr(
        module,
        "run_docker",
        lambda *a, **kw: json.dumps(
            [{"Id": NEW, "Config": {"Labels": {"org.opencontainers.image.version": "0.7.0"}}}]
        ),
    )
    with pytest.raises(DeploymentError, match="metadata"):
        inspect_image(NEW, "0.6.0")


def test_disk_and_unhealthy_preflight_refuse_mutation(releases, monkeypatch):
    monkeypatch.setattr(module.shutil, "disk_usage", lambda path: Mock(free=1))
    with pytest.raises(DeploymentError, match="disk"):
        releases.update("alice", "0.6.0")
    releases.backups._create.assert_not_called()
    monkeypatch.setattr(releases.manager, "doctor", lambda slug: {"ok": False})
    with pytest.raises(DeploymentError, match="doctor"):
        releases.update("alice", "0.6.0")


def test_crash_before_metadata_commit_can_rollback(releases):
    releases.update("alice", "0.6.0")
    state = releases.manager.state("alice")
    state["release_attempt"]["status"] = "RUNNING"
    state["current_version"] = "0.5.0"
    state["operation"] = "update"
    releases.manager.save("alice", state, "UPDATING", "validation")
    assert releases.rollback("alice")["status"] == "OK"


def test_two_client_rollout_and_idempotent_retry(releases, monkeypatch):
    manager = releases.manager  # noqa: F811
    manager.create("bob", request("bob", request().token.replace("123456789", "123456780")))
    before = manager.state("bob")
    env_before = (manager.directory("bob") / ".env").read_bytes()
    validate = releases.validate
    observed = []

    def check_peer(slug):
        observed.append(slug)
        if slug == "alice":
            assert manager.state("bob") == before
            assert (manager.directory("bob") / ".env").read_bytes() == env_before
        validate(slug)

    monkeypatch.setattr(releases, "validate", check_peer)
    assert all(e["status"] == "OK" for e in releases.update_all("0.6.0"))
    assert observed == ["alice", "bob"]
    assert manager.state("bob")["current_version"] == "0.6.0"
    assert all(e["status"] == "ALREADY_CURRENT" for e in releases.update_all("0.6.0"))


def test_failed_rollout_preserves_other_client_files(releases, monkeypatch):
    manager = releases.manager  # noqa: F811
    manager.create("bob", request("bob", request().token.replace("123456789", "123456780")))
    before = {p: p.read_bytes() for p in manager.directory("bob").iterdir() if p.is_file()}
    releases.calls.clear()
    monkeypatch.setattr(releases, "validate", Mock(side_effect=DeploymentError("readiness")))
    with pytest.raises(DeploymentError, match="Update FAILED"):
        releases.update_all("0.6.0")
    after = {p: p.read_bytes() for p in manager.directory("bob").iterdir() if p.is_file()}
    assert before == after
    assert not any(c[0] == "bob" for c in releases.calls)


def test_uncertain_migration_requires_restore_even_when_revision_is_old(releases, monkeypatch):
    original = releases.manager.compose

    def compose(slug, *args, **kwargs):
        if "release-check" in args:
            return json.dumps(
                {"heads": ["future"], "current": [REVISION], "version": "0.6.0", "forward": True}
            )
        if "upgrade" in args:
            raise DeploymentError("migration interrupted")
        return original(slug, *args, **kwargs)

    monkeypatch.setattr(releases.manager, "compose", compose)
    with pytest.raises(DeploymentError, match="Update FAILED"):
        releases.update("alice", "0.6.0")
    assert (
        releases.manager.state("alice")["release_attempt"]["recovery"]
        == "database restore required"
    )
    with pytest.raises(DeploymentError, match="Database restore required"):
        releases.rollback("alice")


@pytest.mark.parametrize("failed", [False, True])
def test_configured_remote_backup_is_inside_lock_and_before_pull(releases, monkeypatch, failed):
    from booking_bot.deployment import backup_remote

    storage = Mock()
    storage.upload.side_effect = AssertionError("Nested public upload lock must not be used")

    def upload(backups, slug, backup_id):
        assert releases.manager.state(slug)["release_attempt"]["backup_id"] == BACKUP
        with pytest.raises(DeploymentError, match="busy"), registry_lock(releases.manager.root):
            pass
        releases.calls.append(("remote_backup",))
        if failed:
            raise DeploymentError("S3 verification failed")

    storage._upload.side_effect = upload
    monkeypatch.setattr(backup_remote, "S3Storage", lambda: storage)
    monkeypatch.setenv("BOOKING_BACKUP_S3_BUCKET", "synthetic-private-bucket")
    if failed:
        with pytest.raises(DeploymentError, match="Update FAILED"):
            releases.update("alice", "0.6.0")
        assert not any(c[0] == "pull" for c in releases.calls)
        assert releases.manager.state("alice")["release_attempt"]["backup_id"] == BACKUP
    else:
        releases.update("alice", "0.6.0")
        upload_pos = releases.calls.index(("remote_backup",))
        pull_pos = next(i for i, c in enumerate(releases.calls) if c[0] == "pull")
        assert releases.calls.index(("backup",)) < upload_pos < pull_pos

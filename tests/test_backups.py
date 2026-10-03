import json
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from unittest.mock import Mock

import pytest

from booking_bot.deployment import backup as module
from booking_bot.deployment.backup import FILES, BackupManager, digest, verify_files
from booking_bot.deployment.backup_schedule import units
from booking_bot.deployment.cli import build_parser, run_backup
from booking_bot.deployment.files import DeploymentError, atomic_write, read_json
from booking_bot.deployment.manager import DeploymentManager
from booking_bot.deployment.templates import compose_model
from test_bookingctl import manager, request  # noqa: F401


@pytest.fixture
def backups(manager, monkeypatch):  # noqa: F811
    manager.create("alice", request())
    instance = BackupManager(manager, manager.root.parent / "backups")
    monkeypatch.setattr(
        module,
        "run_docker",
        lambda *a, **k: json.dumps(
            [{"Config": {"Labels": {"org.opencontainers.image.version": "0.5.0"}}}]
        ),
    )
    monkeypatch.setattr(
        instance,
        "sql",
        lambda slug, query, *a: (
            "170009"
            if "server_version" in query
            else slug
            if "businesses" in query
            else "b62f3d910ea4"
        ),
    )
    monkeypatch.setattr(
        instance,
        "transfer",
        lambda slug, path, **kw: None if kw.get("restore") else path.write_bytes(b"PGDMPtest"),
    )
    return instance


def resign(path):
    atomic_write(path / "SHA256SUMS", "".join(f"{digest(path / n)}  {n}\n" for n in FILES))


def test_manifest_and_no_secrets(backups):
    backup_id = backups.create("alice")
    path = backups.directory("alice", backup_id)
    manifest = backups.verify("alice", backup_id)
    assert manifest["application_version"] == "0.5.0"
    assert manifest["alembic_revision"] == "b62f3d910ea4"
    assert manifest["deployment_identity"] == backups.manager.state("alice")["project"]
    assert set(p.name for p in path.iterdir()) == {*FILES, "SHA256SUMS"}
    for name in FILES[1:]:
        assert request().token not in (path / name).read_text(encoding="utf-8")
    assert backups.list("alice")[0]["integrity"] == "OK"
    assert not backups.status("alice")["warning"]


@pytest.mark.parametrize("name", FILES)
def test_corruption_refused(backups, name):
    backup_id = backups.create("alice")
    path = backups.directory("alice", backup_id)
    with (path / name).open("ab") as stream:
        stream.write(b"corrupt")
    with pytest.raises(DeploymentError, match="checksum"):
        backups.verify("alice", backup_id)
    assert backups.list("alice")[0]["integrity"] == "FAILED"


def test_wrong_slug_even_with_valid_checksums(backups):
    backup_id = backups.create("alice")
    with pytest.raises(DeploymentError):
        verify_files(backups.directory("alice", backup_id), "bob")


@pytest.mark.parametrize("backup_id", ["../secret", "/tmp", "..", "20261001T000000Z-nope"])
def test_path_traversal(backups, backup_id):
    with pytest.raises(DeploymentError):
        backups.directory("alice", backup_id)


def test_unexpected_file_rejected(backups):
    backup_id = backups.create("alice")
    (backups.directory("alice", backup_id) / ".env").write_text("secret")
    with pytest.raises(DeploymentError):
        backups.verify("alice", backup_id)


def test_invalid_config_with_matching_checksum_is_reported(backups):
    backup_id = backups.create("alice")
    path = backups.directory("alice", backup_id)
    atomic_write(path / "specialist.toml", "invalid = [")
    resign(path)
    with pytest.raises(DeploymentError, match="Invalid backup"):
        backups.verify("alice", backup_id)
    assert backups.list("alice")[0]["integrity"] == "FAILED"


def test_verify_failure_never_publishes_backup(backups, monkeypatch):
    monkeypatch.setattr(backups, "_verify_path", Mock(side_effect=DeploymentError("bad dump")))
    with pytest.raises(DeploymentError):
        backups.create("alice")
    assert backups.list("alice") == []


def test_multiple_revisions_rejected(backups, monkeypatch):
    monkeypatch.setattr(backups, "sql", lambda *a: "a\nb")
    with pytest.raises(DeploymentError, match="single"):
        backups.create("alice")


def test_unmanaged_compose_rejected(backups):
    atomic_write(backups.manager.directory("alice") / "compose.yaml", "{}")
    with pytest.raises(DeploymentError, match="Compose"):
        backups.create("alice")


def restore_ready(backups, monkeypatch):
    backup_id = backups.create("alice")
    original = backups.manager.compose

    def compose(slug, *args, **kwargs):
        if "-c" in args and "get_heads" in args[-1]:
            return '["b62f3d910ea4"]'
        return original(slug, *args, **kwargs)

    monkeypatch.setattr(backups.manager, "compose", compose)
    monkeypatch.setattr(
        backups.manager,
        "doctor",
        lambda slug: {
            "checks": [{"check": "registry", "ok": False}, {"check": "postgres", "ok": True}]
        },
    )
    return backup_id


def test_restore_prebackup_config_and_success(backups, monkeypatch):
    backup_id = restore_ready(backups, monkeypatch)
    manager = backups.manager  # noqa: F811
    config = manager.directory("alice") / "specialist.toml"
    saved = config.read_bytes()
    config.write_bytes(saved + b"\n# changed\n")
    backups.restore("alice", backup_id, yes=True)
    assert config.read_bytes() == saved
    state = manager.state("alice")
    assert state["status"] == "READY" and "operation" not in state
    assert state["safety_backup"].startswith("pre-restore-")
    assert state["current_version"] == "0.5.0"
    assert state["release_history"][-1]["operation"] == "restore"
    backups.verify("alice", state["safety_backup"])
    assert any("dropdb" in call for call in manager.calls)


def test_restore_wrong_identity_before_safety(backups, monkeypatch):
    backup_id = restore_ready(backups, monkeypatch)
    state = backups.manager.state("alice")
    state["bot_id"] += 1
    backups.manager.save("alice", state, "READY", "complete")
    create = Mock()
    monkeypatch.setattr(backups, "_create", create)
    with pytest.raises(DeploymentError, match="identity"):
        backups.restore("alice", backup_id, yes=True)
    create.assert_not_called()


def test_safety_backup_failure_blocks_restore(backups, monkeypatch):
    backup_id = restore_ready(backups, monkeypatch)
    monkeypatch.setattr(backups, "_create", Mock(side_effect=DeploymentError("DB unavailable")))
    with pytest.raises(DeploymentError, match="DB unavailable"):
        backups.restore("alice", backup_id, yes=True)
    assert not any("dropdb" in call for call in backups.manager.calls)


def test_failed_restore_stays_failed_and_stopped(backups, monkeypatch):
    backup_id = restore_ready(backups, monkeypatch)
    original = backups.transfer

    def transfer(slug, path, **kw):
        if kw.get("restore"):
            raise DeploymentError("bad restore")
        return original(slug, path, **kw)

    monkeypatch.setattr(backups, "transfer", transfer)
    with pytest.raises(DeploymentError, match="Safety backup: pre-restore"):
        backups.restore("alice", backup_id, yes=True)
    state = backups.manager.state("alice")
    assert state["status"] == "FAILED" and state["operation"] == "restore"
    assert backups.manager.calls[-2] == ("stop", "api", "worker")
    with pytest.raises(DeploymentError, match="Restore interrupted"):
        backups.manager.create("alice", resume=True)


def test_doctor_failure_not_success(backups, monkeypatch):
    backup_id = restore_ready(backups, monkeypatch)
    monkeypatch.setattr(
        backups.manager, "doctor", lambda slug: {"checks": [{"check": "api", "ok": False}]}
    )
    with pytest.raises(DeploymentError):
        backups.restore("alice", backup_id, yes=True)
    assert backups.manager.state("alice")["status"] == "FAILED"


def test_confirmation_cancel_is_readonly(backups, monkeypatch):
    backup_id = restore_ready(backups, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    count = len(backups.list("alice"))
    with pytest.raises(DeploymentError, match="cancelled"):
        backups.restore("alice", backup_id)
    assert len(backups.list("alice")) == count


def test_image_and_schema_mismatch(backups, monkeypatch):
    backup_id = restore_ready(backups, monkeypatch)
    path = backups.directory("alice", backup_id)
    manifest = read_json(path / "manifest.json")
    manifest["alembic_revision"] = "unknown_revision"
    atomic_write(path / "manifest.json", json.dumps(manifest))
    resign(path)
    with pytest.raises(DeploymentError, match="single Alembic head"):
        backups.restore("alice", backup_id, yes=True)


def test_retention_preserves_last_valid_and_safety(backups):
    ids = []
    for index in range(4):
        backup_id = backups.create("alice", safety=index == 1)
        path = backups.directory("alice", backup_id)
        manifest = read_json(path / "manifest.json")
        manifest["created_at"] = (datetime.now(UTC) - timedelta(days=40 + index)).isoformat()
        atomic_write(path / "manifest.json", json.dumps(manifest))
        resign(path)
        ids.append(backup_id)
    assert backups.status("alice")["warning"]
    removed = backups.retain("alice", daily=0, weekly=0, monthly=0)
    assert set(removed) == set(ids[2:])
    assert len(backups.list("alice")) == 2
    assert backups.retain("alice", daily=0, weekly=0, monthly=0) == []


def test_retention_bounds_old_safety_and_preserves_recovery_reference(backups):
    ids = []
    for index in range(5):
        backup_id = backups.create("alice", safety=True)
        path = backups.directory("alice", backup_id)
        manifest = read_json(path / "manifest.json")
        manifest["created_at"] = (datetime.now(UTC) - timedelta(days=40 + index)).isoformat()
        atomic_write(path / "manifest.json", json.dumps(manifest))
        resign(path)
        ids.append(backup_id)
    state = backups.manager.state("alice")
    state["safety_backup"] = ids[-1]
    backups.manager.save("alice", state, "READY", "complete")
    assert backups.retain("alice", daily=0, weekly=0, monthly=0, safety=2) == ids[2:4]
    assert {row["id"] for row in backups.list("alice")} == {ids[0], ids[1], ids[-1]}


def test_schedule_utc_and_escaping():
    service, timer = units(
        PurePosixPath("/opt/booking deployments"),
        PurePosixPath("/backups"),
        "/venv/bookingctl",
        "02:30",
    )
    assert '"/opt/booking deployments"' in service
    assert "UMask=0077" in service and "backup-all" in service
    assert "02:30:00 UTC" in timer
    with pytest.raises(DeploymentError):
        units(Path("/x"), Path("/b"), "/exe", "25:00")


def test_retention_keeps_update_recovery_checkpoint(backups):
    ids = [backups.create("alice") for _ in range(3)]
    for index, backup_id in enumerate(ids):
        path = backups.directory("alice", backup_id)
        manifest = read_json(path / "manifest.json")
        manifest["created_at"] = (datetime.now(UTC) - timedelta(days=40 + index)).isoformat()
        atomic_write(path / "manifest.json", json.dumps(manifest))
        resign(path)
    state = backups.manager.state("alice")
    state["release_attempt"] = {"backup_id": ids[2]}
    backups.manager.save("alice", state, "FAILED", "update_failed")
    assert backups.retain("alice", daily=0, weekly=0, monthly=0) == [ids[1]]
    assert backups.verify("alice", ids[2])


def test_interrupted_backup_can_resume_and_restore_previous_status(backups):
    state = backups.manager.state("alice")
    state["backup_previous_state"] = {"status": "READY", "stage": "complete"}
    backups.manager.save("alice", state, "BACKING_UP", "backup")
    assert backups.verify("alice", backups.create("alice"))
    state = backups.manager.state("alice")
    assert state["status"] == "READY" and "backup_previous_state" not in state


def test_backup_all_continues_on_failure(tmp_path, monkeypatch):
    manager = Mock(spec=DeploymentManager)  # noqa: F811
    manager.root = tmp_path / "registry"
    manager.list.return_value = [{"slug": "alice"}, {"slug": "bob"}]
    create = Mock(side_effect=[DeploymentError("failed"), "second-ok"])
    monkeypatch.setattr(BackupManager, "create", create)
    monkeypatch.delenv("BOOKING_BACKUP_S3_BUCKET", raising=False)
    args = build_parser().parse_args(["backup-all"])
    with pytest.raises(DeploymentError, match="alice"):
        run_backup(args, manager)
    assert create.call_count == 2


def test_last_valid_backup_never_deleted(backups):
    backup_id = backups.create("alice")
    assert backups.retain("alice", daily=0, weekly=0, monthly=0) == []
    assert backups.directory("alice", backup_id).is_dir()


def test_recover_files_keeps_original_identity(backups, monkeypatch):
    backup_id = backups.create("alice")
    original = backups.manager.state("alice")
    new = DeploymentManager(backups.manager.root.parent / "recovered-registry")
    recovery = BackupManager(new, backups.root)
    monkeypatch.setattr(
        module, "get_bot_identity", lambda token: type("Identity", (), {"id": original["bot_id"]})()
    )
    monkeypatch.setattr(module, "resolve_image", lambda ref: original["image_id"])
    recovery.recover_files("alice", backup_id, request().token)
    state = new.state("alice")
    assert state["project"] == original["project"] and state["operation"] == "restore"
    assert read_json(new.directory("alice") / "compose.yaml") == compose_model(state["project"])
    assert (
        new.values("alice")["POSTGRES_PASSWORD"]
        != backups.manager.values("alice")["POSTGRES_PASSWORD"]
    )


def test_backup_root_cannot_be_registry(tmp_path):
    with pytest.raises(DeploymentError):
        BackupManager(DeploymentManager(tmp_path), tmp_path / "backups")


def test_empty_doctor_cannot_mark_restore_healthy(backups, monkeypatch):
    backup_id = restore_ready(backups, monkeypatch)
    monkeypatch.setattr(backups.manager, "doctor", lambda slug: {"checks": []})
    with pytest.raises(DeploymentError, match="Restore failed"):
        backups.restore("alice", backup_id, yes=True)
    assert backups.manager.state("alice")["status"] == "FAILED"


def test_public_recovery_webhook_failure_remains_recoverable(backups, monkeypatch):
    state = backups.manager.state("alice")
    state.update(public=True, domain="alice.example.org", recovery_requires_webhook=True)
    backups.manager.save("alice", state, "READY", "complete")
    atomic_write(
        backups.manager.directory("alice") / "compose.yaml",
        json.dumps(compose_model(state["project"], state["domain"], public=True)),
    )
    backup_id = restore_ready(backups, monkeypatch)
    original = backups.manager.compose

    def compose(slug, *args, **kwargs):
        if "set-webhook" in args:
            raise DeploymentError("Telegram unavailable")
        return original(slug, *args, **kwargs)

    monkeypatch.setattr(backups.manager, "compose", compose)
    with pytest.raises(DeploymentError, match="Restore failed"):
        backups.restore("alice", backup_id, yes=True)
    assert backups.manager.state("alice")["recovery_requires_webhook"]
    monkeypatch.setattr(backups.manager, "compose", original)
    backups.restore("alice", backup_id, yes=True)
    assert any("set-webhook" in call for call in backups.manager.calls)
    assert "recovery_requires_webhook" not in backups.manager.state("alice")

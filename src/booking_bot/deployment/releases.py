"""Explicit, serial releases. Never downgrade a database or automatically recover a failure."""

import json
import os
import re
import shutil
from contextlib import nullcontext
from datetime import UTC, datetime

from booking_bot.deployment.backup import BackupManager
from booking_bot.deployment.files import (
    DeploymentError,
    atomic_write,
    operation_lock,
    registry_lock,
)
from booking_bot.deployment.manager import DeploymentManager, run_docker
from booking_bot.version import (
    IMAGE_REPOSITORY,
    parse_current_version,
    parse_release_version,
    version_order,
)


def version(value: str) -> str:
    try:
        parse_release_version(value)
    except (ValueError, TypeError):
        raise DeploymentError("Use MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH-rc.N (N >= 1)") from None
    return value


def inspect_image(reference: str, expected: str | None = None) -> dict:
    try:
        item = json.loads(run_docker(["image", "inspect", reference], timeout=30))[0]
        labels = item["Config"].get("Labels") or {}
        release = labels["org.opencontainers.image.version"]
        if expected:
            version(release)
        else:
            parse_current_version(release)
        image_id = item["Id"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError
        if expected and (
            release != expected
            or not re.fullmatch(
                r"[0-9a-f]{40}", labels.get("org.opencontainers.image.revision", "")
            )
            or labels.get("org.opencontainers.image.source")
            != ("https://github.com/Polen452294/Booking_bot")
        ):
            raise ValueError
        return {"id": image_id, "version": release, "labels": labels}
    except (ValueError, KeyError, IndexError, TypeError):
        raise DeploymentError("Image version/revision/source metadata is invalid") from None


def target_reference(target: str) -> str:
    return f"{IMAGE_REPOSITORY}:{version(target)}"


class ReleaseManager:
    def __init__(self, manager: DeploymentManager, backup_root=None):
        self.manager = manager
        self.backups = BackupManager(manager, backup_root)

    def available(self, target: str, *, dry_run: bool) -> dict:
        reference = target_reference(target)
        if dry_run:
            # Registry read only: no pull, no files, no DB changes. Fail on unavailable images.
            run_docker(["manifest", "inspect", reference], timeout=60)
            return {"reference": reference}
        run_docker(["pull", reference], timeout=600)
        image = inspect_image(reference, target)
        return {**image, "reference": reference}

    def migrations(self, slug: str, image_id: str) -> dict:
        output = self.manager.compose(
            slug,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "admin",
            "python",
            "-m",
            "booking_bot.deployment.runtime",
            "release-check",
            image=image_id,
            timeout=60,
        )
        try:
            result = json.loads(output)
            if len(result["heads"]) != 1 or len(result["current"]) != 1 or not result["forward"]:
                raise ValueError
            version(result["version"])
            return result
        except (KeyError, ValueError, TypeError):
            raise DeploymentError(
                "Migration preflight failed: single forward head required"
            ) from None

    def preflight(self, slug: str, target: str) -> tuple[dict, dict]:
        manager = self.manager
        state = manager.state(slug)
        if state["status"] != "READY" or state.get("operation"):
            raise DeploymentError("Deployment is not READY; inspect status/doctor/history first")
        manager.assert_no_admin(slug)
        self.backups.managed_config(slug)
        report = manager.doctor(slug)
        if not report.get("ok"):
            raise DeploymentError("Pre-update doctor failed; inspect doctor before updating")
        current = inspect_image(state["image_id"])
        if state.get("current_version", current["version"]) != current["version"]:
            raise DeploymentError("Confirmed version differs from current image metadata")
        if version_order(target) <= version_order(current["version"]):
            raise DeploymentError("Update requires a newer version; use rollback for recovery")
        # Logical dump size estimate plus 1GiB reserve on both host filesystems.
        size = int(self.backups.sql(slug, "SELECT pg_database_size(current_database())"))
        for path in (manager.directory(slug), self.backups.root):
            while not path.exists():
                path = path.parent
            if shutil.disk_usage(path).free < size * 2 + 1024**3:
                raise DeploymentError("Insufficient host disk space for backup/update")
        docker_root = run_docker(["info", "--format", "{{.DockerRootDir}}"], timeout=20).strip()
        # Production Linux: check the filesystem actually storing Docker layers/volumes.
        if os.name != "nt":
            if not docker_root or not os.path.isdir(docker_root):
                raise DeploymentError("Cannot verify Docker storage disk space")
            if shutil.disk_usage(docker_root).free < size * 2 + 1024**3:
                raise DeploymentError("Insufficient Docker storage disk space")
        if not shutil.which("docker"):
            raise DeploymentError("Backup tooling unavailable")
        return state, current

    def select_image(self, slug: str, state: dict, image: dict) -> None:
        # Persist intent before changing .env. A crash is visible as an unfinished operation.
        self.manager.refresh_compose(slug)
        state.update(image_id=image["id"], image_reference=image["reference"])
        self.manager.save(slug, state, "UPDATING", "select_image")
        values = self.manager.values(slug)
        values["BOOKING_IMAGE"] = image["id"]
        atomic_write(
            self.manager.directory(slug) / ".env",
            "".join(f"{key}={value}\n" for key, value in values.items()),
        )

    def validate(self, slug: str) -> None:
        manager = self.manager
        manager.compose(slug, "up", "-d", "--wait", "--wait-timeout", "120", "postgres", "redis")
        manager.compose(
            slug,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "180",
            "api",
            "worker",
        )
        for route in ("live", "ready"):
            manager.compose(
                slug,
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                "import urllib.request; "
                f"urllib.request.urlopen('http://localhost:8000/{route}',timeout=4)",
            )
        manager.compose(slug, "exec", "-T", "worker", "booking-admin", "worker-health")
        if manager.state(slug).get("public"):
            manager.compose(
                slug,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "admin",
                "booking-admin",
                "webhook-status",
                timeout=60,
            )
        report = manager.doctor(slug)
        checks = [c for c in report.get("checks", []) if c["check"] != "registry"]
        atomic_write(manager.directory(slug) / "update-health.json", json.dumps(report))
        if not checks or not all(c["ok"] for c in checks):
            raise DeploymentError("Post-update doctor failed")

    def history(self, slug: str) -> list[dict]:
        return self.manager.state(slug).get("release_history", [])

    def update(self, slug: str, target: str, *, dry_run: bool = False) -> dict:
        version(target)
        lock = (
            nullcontext()
            if dry_run
            else operation_lock(self.manager.root, self.manager.directory(slug))
        )
        with lock:
            return self._update(slug, target, dry_run=dry_run)

    def _update(self, slug: str, target: str, *, dry_run: bool = False) -> dict:
        state, current = self.preflight(slug, target)
        reference = target_reference(target)
        # Read registry availability before backup, download only after verified backup.
        run_docker(["manifest", "inspect", reference], timeout=60)
        revision = self.backups.sql(slug, "SELECT version_num FROM alembic_version")
        if dry_run:
            return {
                "client": slug,
                "current_version": current["version"],
                "target_version": target,
                "image": reference,
                "backup": "mandatory verified pg_dump before pull",
                "migration_state": {
                    "current": revision,
                    "target": "validated after backup and image pull",
                },
                "affected_services": ["api", "worker", "admin"],
                "dry_run": True,
            }
        event = {
            "operation": "update",
            "from": current["version"],
            "to": target,
            "started_at": datetime.now(UTC).isoformat(),
            "status": "RUNNING",
            "backup_id": None,
            "migration_status": "not_started",
            "recovery": "application-only rollback",
        }
        previous = {
            "id": state["image_id"],
            "reference": state["image_reference"],
            "version": current["version"],
            "revision": revision,
        }
        state.update(
            operation="update",
            current_version=current["version"],
            release_attempt=event,
            previous_release=previous,
        )
        self.manager.save(slug, state, "BACKING_UP", "update_backup")
        try:
            event["backup_id"] = self.backups._create(slug)
            self.manager.save(slug, state, "BACKING_UP", "backup_verified")
            if os.environ.get("BOOKING_BACKUP_S3_BUCKET"):
                from booking_bot.deployment.backup_remote import S3Storage

                S3Storage()._upload(self.backups, slug, event["backup_id"])
            self.manager.save(slug, state, "UPDATING", "pull")
            target_image = self.available(target, dry_run=False)
            migration = self.migrations(slug, target_image["id"])
            if migration["version"] != target or migration["current"] != [revision]:
                raise DeploymentError("Target package/version/revision mismatch")
            event["expected_revision"] = migration["heads"][0]
            self.manager.save(slug, state, "UPDATING", "stop")
            self.manager.compose(slug, "stop", "worker", "api")
            self.select_image(slug, state, target_image)
            # Conservatively require restore as soon as a migration may have started.
            if migration["heads"] != [revision]:
                event["recovery"] = "database restore required"
            event["migration_status"] = "started"
            self.manager.save(slug, state, "UPDATING", "migration")
            self.manager.compose(
                slug, "run", "--rm", "--no-deps", "-T", "admin", "alembic", "upgrade", "head"
            )
            event["migration_status"] = "completed"
            self.manager.save(slug, state, "UPDATING", "validation")
            self.validate(slug)
            state.update(
                previous_version=current["version"],
                current_version=target,
                backup_id=event["backup_id"],
            )
            event["status"] = "OK"
            state.pop("operation", None)
            self.finish(slug, state, event, "READY")
            return event
        except BaseException:
            event["status"] = "FAILED"
            # Avoid serving an unvalidated release. Never stop another client's services.
            if state["image_id"] != previous["id"]:
                try:
                    self.manager.compose(slug, "stop", "worker", "api")
                except Exception:
                    pass
            self.finish(slug, state, event, "FAILED")
            raise DeploymentError(
                f"Update FAILED; old={current['version']} new={target}; "
                f"backup={event['backup_id'] or 'NONE'}; migration={event['migration_status']}; "
                f"recovery={event['recovery']}. Inspect status/history; use rollback "
                "(or rollback --restore-database --yes after reviewing the backup)."
            ) from None

    def finish(self, slug: str, state: dict, event: dict, status: str) -> None:
        event["finished_at"] = datetime.now(UTC).isoformat()
        state.setdefault("release_history", []).append(dict(event))
        self.manager.save(slug, state, status, "complete" if status == "READY" else "update_failed")

    def rollback(self, slug: str, *, restore_database: bool = False, yes: bool = False) -> dict:
        with operation_lock(self.manager.root, self.manager.directory(slug)):
            state = self.manager.state(slug)
            if state.get("operation") not in {None, "update", "rollback"}:
                raise DeploymentError("Finish the active operation before rollback")
            previous = state.get("previous_release")
            attempt = state.get("release_attempt")
            if (
                not previous
                or not attempt
                or attempt.get("status") not in {"OK", "FAILED", "RUNNING"}
            ):
                raise DeploymentError("No completed/failed release is available for rollback")
            self.manager.assert_no_admin(slug)
            old = inspect_image(previous["id"])
            if old["version"] != previous["version"]:
                raise DeploymentError("Previous image metadata mismatch")
            revision = self.backups.sql(slug, "SELECT version_num FROM alembic_version")
            restore_required = (
                revision != previous["revision"]
                or attempt["recovery"] == "database restore required"
            )
            if restore_required and not restore_database:
                raise DeploymentError(
                    "Database restore required; image rollback cannot undo schema"
                )
            if restore_database and not yes:
                raise DeploymentError(
                    "Database restore overwrites data; review backup and pass --yes"
                )
            if restore_database:
                manifest = self.backups.verify(slug, attempt["backup_id"])
                if manifest["image_id"] != previous["id"]:
                    raise DeploymentError("Recovery backup image mismatch")
            event = {
                "operation": "rollback",
                "from": state.get("current_version"),
                "to": previous["version"],
                "status": "RUNNING",
                "backup_id": attempt["backup_id"],
                "database_restored": restore_database,
            }
            state["operation"] = "rollback"
            self.manager.save(slug, state, "UPDATING", "rollback")
            try:
                if restore_database:
                    # Capture current DB under its CURRENT image before selecting recovery image.
                    safety = self.backups._create(slug, safety=True)
                    state["safety_backup"] = safety
                self.manager.compose(slug, "stop", "worker", "api")
                self.select_image(slug, state, previous)
                if restore_database:
                    self.backups._restore(
                        slug, attempt["backup_id"], yes=True, safety_backup=safety
                    )
                    state = self.manager.state(slug)
                self.validate(slug)
                state.update(
                    previous_version=event["from"],
                    current_version=previous["version"],
                    backup_id=attempt["backup_id"],
                )
                state.pop("operation", None)
                state.pop("previous_release", None)
                event["status"] = "OK"
                self.finish(slug, state, event, "READY")
                return event
            except BaseException:
                event["status"] = "FAILED"
                self.finish(slug, state, event, "FAILED")
                try:
                    self.manager.compose(slug, "stop", "worker", "api")
                except Exception:
                    pass
                raise DeploymentError(
                    "Rollback FAILED; services stopped where possible; inspect status"
                ) from None

    def update_all(self, target: str, *, dry_run: bool = False) -> list[dict]:
        version(target)
        # Same common registry lock as backup/restore/update: two rollouts cannot overlap.
        with nullcontext() if dry_run else registry_lock(self.manager.root):
            results = []
            for state in self.manager.list():
                if state.get("current_version") == target and state["status"] == "READY":
                    if state.get("operation") or not self.manager.doctor(state["slug"]).get("ok"):
                        raise DeploymentError(
                            "Already-current client is unhealthy; rollout stopped"
                        )
                    results.append({"client": state["slug"], "status": "ALREADY_CURRENT"})
                    continue
                lock = (
                    nullcontext()
                    if dry_run
                    else registry_lock(self.manager.directory(state["slug"]))
                )
                with lock:
                    results.append(self._update(state["slug"], target, dry_run=dry_run))
            return results

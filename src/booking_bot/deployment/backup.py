"""Isolated logical backups. Registry lock serializes all supported schema/config writers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from booking_bot.deployment.files import (
    DeploymentError,
    atomic_write,
    operation_lock,
    private_directory,
    read_json,
    registry_lock,
)
from booking_bot.deployment.manager import (
    SECRET_KEYS,
    DeploymentManager,
    docker_environment,
    get_bot_identity,
    resolve_image,
    run_docker,
    validate_domain,
    validate_slug,
)
from booking_bot.deployment.templates import compose_model, legacy_compose_model
from booking_bot.specialist_config import SpecialistConfigError, load_specialist_template

FILES = ("database.dump", "specialist.toml", "metadata.json", "manifest.json")
ALL_FILES = (*FILES, "SHA256SUMS")
STATE_KEYS = (
    "slug",
    "project",
    "bot_id",
    "bot_username",
    "image_id",
    "image_reference",
    "domain",
    "public",
)
ID_PATTERN = r"(?:pre-restore-)?\d{8}T\d{6}Z-[0-9a-f]{8}"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_path(path: Path) -> Path:
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise DeploymentError("Backup paths must not contain links")
    return path


def verify_files(path: Path, slug: str) -> dict:
    """No Docker needed; strict names prevent traversal and accidental secret inclusion."""
    safe_path(path)
    try:
        if {p.name for p in path.iterdir()} != set(ALL_FILES):
            raise ValueError
        for name in ALL_FILES:
            p = safe_path(path / name)
            if not p.is_file() or p.stat().st_nlink != 1:
                raise ValueError
        expected = "".join(f"{digest(path / name)}  {name}\n" for name in FILES)
        if (path / "SHA256SUMS").read_text() != expected:
            raise DeploymentError("Backup checksum mismatch; restore forbidden")
        manifest = read_json(path / "manifest.json")
        metadata = read_json(path / "metadata.json")
        state = metadata["deployment"]
        if (
            manifest["format_version"] != 1
            or manifest["database_format"] != "pg_dump_custom"
            or manifest["client_slug"] != slug
            or state["slug"] != slug
            or manifest["deployment_identity"] != state["project"]
            or not re.fullmatch(rf"booking-{re.escape(slug)}-[0-9a-f]{{8}}", state["project"])
            or set(state) != set(STATE_KEYS)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", state["image_id"])
            or manifest["image_id"] != state["image_id"]
            or manifest["domain"] != state["domain"]
            or not isinstance(manifest["application_version"], str)
            or not re.fullmatch(r"[a-zA-Z0-9_]+", manifest["alembic_revision"])
            or not str(manifest["postgres_version_num"]).isdigit()
        ):
            raise ValueError
        validate_domain(state["domain"])
        created = datetime.fromisoformat(manifest["created_at"])
        if created.utcoffset() != timedelta(0) or created > datetime.now(UTC) + timedelta(
            minutes=5
        ):
            raise ValueError
        if load_specialist_template(path / "specialist.toml").profile.slug != slug:
            raise ValueError
        with (path / "database.dump").open("rb") as stream:
            if stream.read(5) != b"PGDMP":
                raise ValueError
        return manifest
    except (OSError, ValueError, KeyError, TypeError, SpecialistConfigError):
        raise DeploymentError("Invalid backup manifest, files or client identity") from None


class BackupManager:
    def __init__(self, manager: DeploymentManager, root: Path | None = None):
        self.manager = manager
        self.root = safe_path(
            (
                root
                or Path(os.environ.get("BOOKING_BACKUP_ROOT", str(manager.root.parent / "backups")))
            )
            .expanduser()
            .absolute()
        )
        if (
            self.root == manager.root
            or self.root.is_relative_to(manager.root)
            or manager.root.is_relative_to(self.root)
        ):
            raise DeploymentError("Backup root must be outside the deployment registry")

    def directory(self, slug: str, backup_id: str | None = None) -> Path:
        path = self.root / validate_slug(slug)
        if backup_id is not None:
            if not re.fullmatch(ID_PATTERN, backup_id):
                raise DeploymentError("Invalid backup ID")
            path /= backup_id
        return safe_path(path)

    def event(self, slug: str, event: str, backup_id: str = "") -> None:
        private_directory(self.root)
        path = safe_path(self.root / "events.jsonl")
        # Contains only validated identifiers and constant event names, never subprocess output.
        with path.open("a", encoding="utf-8") as stream:
            if os.name != "nt":
                path.chmod(0o600)
            stream.write(
                json.dumps(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "client": slug,
                        "event": event,
                        "backup_id": backup_id,
                    }
                )
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        if event in {"backup failed", "backup completed", "restore failed", "restore completed"}:
            from booking_bot.deployment.alerts import operation_event

            try:
                operation_event(
                    self.manager,
                    slug,
                    event.split()[0],
                    failed=event.endswith("failed"),
                    backup_root=self.root,
                )
            except (DeploymentError, OSError):
                # Preserve the primary backup result if optional alert persistence fails.
                import logging

                logging.getLogger(__name__).warning("Operation alert state unavailable")

    def sql(self, slug: str, query: str, database: str = "booking") -> str:
        return self.manager.compose(
            slug,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "booking",
            "-d",
            database,
            "-Atqc",
            query,
        ).strip()

    def transfer(self, slug: str, path: Path, *, restore: bool = False) -> None:
        """Stream binary data directly, without text decoding or shell redirection."""
        manager = self.manager
        container = manager.compose(slug, "ps", "-q", "postgres").strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", container):
            raise DeploymentError("Expected exactly one PostgreSQL container")
        info = json.loads(run_docker(["inspect", container]))[0]
        labels = info["Config"]["Labels"]
        if (
            labels.get("com.docker.compose.project") != manager.state(slug)["project"]
            or labels.get("com.docker.compose.service") != "postgres"
        ):
            raise DeploymentError("PostgreSQL container identity mismatch")
        command = (
            [
                "pg_restore",
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-privileges",
                "-U",
                "booking",
                "-d",
                "booking",
            ]
            if restore
            else [
                "pg_dump",
                "-Fc",
                "--no-owner",
                "--no-privileges",
                "--lock-wait-timeout=30s",
                "-U",
                "booking",
                "-d",
                "booking",
            ]
        )
        with path.open("rb" if restore else "xb") as stream:
            if not restore and os.name != "nt":
                path.chmod(0o600)
            try:
                result = subprocess.run(
                    ["docker", "exec", "-i", container, *command],
                    stdin=stream if restore else subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL if restore else stream,
                    stderr=subprocess.PIPE,
                    env=docker_environment(),
                    timeout=3600,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise DeploymentError(
                    "Database transfer failed or timed out; inspect restore state"
                ) from None
            if not restore:
                stream.flush()
                os.fsync(stream.fileno())
        if result.returncode:
            atomic_write(
                manager.directory(slug) / "last-error.log",
                manager.redact(slug, result.stderr.decode("utf-8", errors="replace")),
            )
            raise DeploymentError("Database transfer failed; see private last-error.log")

    def verify(self, slug: str, backup_id: str) -> dict:
        path = self.directory(slug, backup_id)
        try:
            manifest = self._verify_path(slug, path)
        except Exception:
            self.event(slug, "verification failed", backup_id)
            raise
        self.event(slug, "verification completed", backup_id)
        return manifest

    def _verify_path(self, slug: str, path: Path) -> dict:
        manifest = verify_files(path, slug)
        # The tooling version is fixed, never taken as an executable/image from untrusted metadata.
        if int(manifest["postgres_version_num"]) // 10000 != 17:
            raise DeploymentError("Only PostgreSQL 17 backups are supported")
        run_docker(
            [
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--mount",
                f"type=bind,src={path},dst=/backup,readonly",
                "postgres:17-alpine",
                "pg_restore",
                "--list",
                "/backup/database.dump",
            ],
            timeout=120,
        )
        return manifest

    def managed_config(self, slug: str) -> None:
        manager = self.manager
        state = manager.state(slug)
        manager.local_config(slug)
        model = compose_model(
            state["project"], state.get("domain", ""), public=state.get("public", False)
        )
        installed = read_json(manager.directory(slug) / "compose.yaml")
        legacy = legacy_compose_model(
            state["project"], state.get("domain", ""), public=state.get("public", False)
        )
        if installed not in (model, legacy):
            raise DeploymentError("Compose differs from managed template; reconcile before backup")

    def create(self, slug: str, *, safety: bool = False) -> str:
        with operation_lock(self.manager.root, self.manager.directory(slug)):
            state = self.manager.state(slug)
            if state.get("operation"):
                raise DeploymentError("Finish the active deployment operation before backup")
            previous = (state["status"], state["stage"])
            if state["status"] == "BACKING_UP":
                saved = state.get("backup_previous_state", {})
                if saved.get("status") not in {"READY", "FAILED", "CREATING"}:
                    raise DeploymentError("Interrupted backup state is unknown; inspect status")
                previous = (saved["status"], saved["stage"])
            state["backup_previous_state"] = {"status": previous[0], "stage": previous[1]}
            self.manager.save(slug, state, "BACKING_UP", "backup")
            try:
                return self._create(slug, safety=safety)
            finally:
                state.pop("backup_previous_state", None)
                self.manager.save(slug, state, *previous)

    def _create(self, slug: str, *, safety: bool = False) -> str:
        manager = self.manager
        state = manager.state(slug)
        self.managed_config(slug)
        manager.assert_no_admin(slug)
        if state.get("operation") == "configure":
            raise DeploymentError("Finish configuration before backup")
        source = manager.directory(slug)
        revision = self.sql(slug, "SELECT version_num FROM alembic_version")
        if not re.fullmatch(r"[a-zA-Z0-9_]+", revision):
            raise DeploymentError("Backup requires a single Alembic revision")
        if self.sql(slug, "SELECT slug FROM businesses") != slug:
            raise DeploymentError("Database belongs to another client")
        pg_version = int(self.sql(slug, "SHOW server_version_num"))
        image = json.loads(run_docker(["image", "inspect", state["image_id"]]))[0]
        version = (image["Config"].get("Labels") or {}).get("org.opencontainers.image.version")
        if not version or version == "unknown":
            raise DeploymentError("Image must have an application version label")
        now = datetime.now(UTC)
        backup_id = ("pre-restore-" if safety else "") + now.strftime("%Y%m%dT%H%M%SZ-")
        backup_id += secrets.token_hex(4)
        private_directory(self.root)
        private_directory(self.directory(slug))
        target = self.directory(slug, backup_id)
        staging = safe_path(target.with_name(".incomplete-" + backup_id))
        private_directory(staging)
        self.event(slug, "backup started", backup_id)
        try:
            self.transfer(slug, staging / "database.dump")
            if self.sql(slug, "SELECT version_num FROM alembic_version") != revision:
                raise DeploymentError("Schema changed during backup")
            atomic_write(
                staging / "specialist.toml",
                (source / "specialist.toml").read_text(encoding="utf-8"),
            )
            metadata = {
                "deployment": {
                    key: state.get(key, False if key == "public" else "") for key in STATE_KEYS
                },
                "storage_images": {
                    key: manager.values(slug)[key] for key in ("POSTGRES_IMAGE", "REDIS_IMAGE")
                },
            }
            atomic_write(staging / "metadata.json", json.dumps(metadata, indent=2))
            manifest = {
                "format_version": 1,
                "client_slug": slug,
                "deployment_identity": state["project"],
                "created_at": now.isoformat(),
                "application_version": version,
                "image_id": state["image_id"],
                "postgres_version_num": pg_version,
                "alembic_revision": revision,
                "domain": state.get("domain", ""),
                "database_format": "pg_dump_custom",
            }
            atomic_write(staging / "manifest.json", json.dumps(manifest, indent=2))
            atomic_write(
                staging / "SHA256SUMS",
                "".join(f"{digest(staging / name)}  {name}\n" for name in FILES),
            )
            self._verify_path(slug, staging)
            staging.rename(target)
            self.event(slug, "backup completed", backup_id)
            return backup_id
        except BaseException:
            self.event(slug, "backup failed", backup_id)
            raise

    def list(self, slug: str) -> list[dict]:
        directory = self.directory(slug)
        if not directory.exists():
            return []
        rows = []
        for path in sorted(directory.iterdir(), reverse=True):
            if not re.fullmatch(ID_PATTERN, path.name):
                continue
            try:
                manifest = verify_files(path, slug)
                rows.append({"id": path.name, "integrity": "OK", **manifest})
            except DeploymentError:
                rows.append({"id": path.name, "integrity": "FAILED"})
        return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)

    def status(self, slug: str) -> dict:
        valid = [row for row in self.list(slug) if row["integrity"] == "OK"]
        if not valid:
            return {"warning": True, "last_backup": None, "integrity": "MISSING"}
        latest = valid[0]
        age = (
            datetime.now(UTC) - datetime.fromisoformat(latest["created_at"])
        ).total_seconds() / 3600
        return {
            "last_backup": latest["id"],
            "created_at": latest["created_at"],
            "age_hours": round(age, 2),
            "integrity": "OK",
            "warning": age > float(os.environ.get("BOOKING_BACKUP_MAX_AGE_HOURS", "36")),
        }

    def compatibility(self, slug: str, backup_id: str) -> dict:
        manifest = self.verify(slug, backup_id)
        state = self.manager.state(slug)
        saved = read_json(self.directory(slug, backup_id) / "metadata.json")["deployment"]
        if any(
            state.get(key, False if key == "public" else "") != saved[key]
            for key in ("slug", "project", "bot_id", "domain", "public")
        ):
            raise DeploymentError("Backup belongs to a different deployment identity/domain")
        if state["image_id"] != manifest["image_id"]:
            raise DeploymentError(
                "Restore requires the exact saved application image; no downgrade"
            )
        self.managed_config(slug)
        output = self.manager.compose(
            slug,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "admin",
            "python",
            "-c",
            "import json; from alembic.config import Config; "
            "from alembic.script import ScriptDirectory; "
            "print(json.dumps(ScriptDirectory.from_config(Config('alembic.ini')).get_heads()))",
        )
        if json.loads(output) != [manifest["alembic_revision"]]:
            raise DeploymentError("Backup revision must match the image's single Alembic head")
        return manifest

    def restore(
        self, slug: str, backup_id: str, *, yes: bool = False, dangerous_skip_safety: bool = False
    ) -> None:
        with operation_lock(self.manager.root, self.manager.directory(slug)):
            self._restore(slug, backup_id, yes=yes, dangerous_skip_safety=dangerous_skip_safety)

    def _restore(
        self,
        slug: str,
        backup_id: str,
        *,
        yes: bool = False,
        dangerous_skip_safety: bool = False,
        safety_backup: str | None = None,
    ) -> None:
        manager = self.manager
        manager.assert_no_admin(slug)
        manifest = self.compatibility(slug, backup_id)
        state = manager.state(slug)
        print(
            json.dumps({"source": manifest, "target": {k: state[k] for k in STATE_KEYS}}, indent=2)
        )
        if dangerous_skip_safety:
            print("DANGER: proceeding without a pre-restore safety backup")
        if not yes and input(f"Type restore {slug} to replace this client's database: ") != (
            f"restore {slug}"
        ):
            raise DeploymentError("Restore cancelled")
        if safety_backup:
            self.verify(slug, safety_backup)
        safety = safety_backup or (
            None if dangerous_skip_safety else self._create(slug, safety=True)
        )
        if safety:
            print(f"Safety backup: {self.directory(slug, safety)}", flush=True)
        state.update(operation="restore", restore_backup=backup_id, safety_backup=safety)
        manager.save(slug, state, "RESTORING", "restore_started")
        self.event(slug, "restore started", backup_id)
        try:
            manager.compose(slug, "stop", "api", "worker")
            manager.compose(slug, "up", "-d", "--wait", "postgres", "redis")
            if int(self.sql(slug, "SHOW server_version_num", "postgres")) // 10000 != 17:
                raise DeploymentError("Target PostgreSQL major version must be 17")
            manager.save(slug, state, "RESTORING", "restore_database")
            manager.compose(
                slug,
                "exec",
                "-T",
                "postgres",
                "dropdb",
                "--if-exists",
                "--force",
                "-U",
                "booking",
                "booking",
            )
            manager.compose(
                slug,
                "exec",
                "-T",
                "postgres",
                "createdb",
                "-U",
                "booking",
                "-O",
                "booking",
                "-T",
                "template0",
                "booking",
            )
            self.transfer(slug, self.directory(slug, backup_id) / "database.dump", restore=True)
            if (
                self.sql(slug, "SELECT version_num FROM alembic_version")
                != manifest["alembic_revision"]
            ):
                raise DeploymentError("Restored Alembic revision differs from manifest")
            if self.sql(slug, "SELECT slug FROM businesses") != slug:
                raise DeploymentError("Restored database client mismatch")
            atomic_write(
                manager.directory(slug) / "specialist.toml",
                (self.directory(slug, backup_id) / "specialist.toml").read_text(encoding="utf-8"),
                public_config=True,
            )
            manager.compose(
                slug, "run", "--rm", "--no-deps", "-T", "admin", "alembic", "upgrade", "head"
            )
            # Redis state refers to the discarded DB timeline; invalidate only this client.
            manager.compose(
                slug,
                "exec",
                "-T",
                "redis",
                "sh",
                "-c",
                'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli FLUSHALL',
            )
            state["config_sha256"] = manager.config_digest(slug)
            manager.save(slug, state, "RESTORING", "restore_healthcheck")
            manager.compose(
                slug,
                "up",
                "-d",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "180",
                "api",
                "worker",
            )
            if state.get("recovery_requires_webhook") and state.get("public"):
                manager.compose(
                    slug,
                    "run",
                    "--rm",
                    "--no-deps",
                    "-T",
                    "admin",
                    "booking-admin",
                    "set-webhook",
                    timeout=60,
                )
            # Doctor's registry check is intentionally false until all health checks pass.
            report = manager.doctor(slug)
            atomic_write(manager.directory(slug) / "restore-health.json", json.dumps(report))
            checks = [c for c in report["checks"] if c["check"] != "registry"]
            if not checks or not all(c["ok"] for c in checks):
                raise DeploymentError("Post-restore doctor failed")
            state.pop("operation", None)
            state.pop("recovery_requires_webhook", None)
            state.setdefault("release_history", []).append(
                {
                    "operation": "restore",
                    "from": state.get("current_version"),
                    "to": manifest["application_version"],
                    "status": "OK",
                    "backup_id": backup_id,
                    "finished_at": datetime.now(UTC).isoformat(),
                }
            )
            state["current_version"] = manifest["application_version"]
            manager.save(slug, state, "READY", "complete")
            self.event(slug, "restore completed", backup_id)
        except BaseException:
            state["operation"] = "restore"
            manager.save(slug, state, "FAILED", "restore_failed")
            try:
                manager.compose(slug, "stop", "api", "worker")
                atomic_write(
                    manager.directory(slug) / "restore-failure.log",
                    manager.logs(slug, None, 150),
                )
            except Exception:
                pass
            self.event(slug, "restore failed", backup_id)
            raise DeploymentError(
                f"Restore failed; services stopped where possible. Safety "
                f"backup: {safety or 'NONE'}. Inspect restore-failure.log and "
                "restore-health.json; recover with bookingctl restore."
            ) from None

    def retain(
        self,
        slug: str,
        *,
        daily: int = 7,
        weekly: int = 4,
        monthly: int = 3,
        safety: int = 3,
    ) -> list[str]:
        if min(daily, weekly, monthly, safety) < 0:
            raise DeploymentError("Retention counts cannot be negative")
        with registry_lock(self.manager.root):
            rows = [r for r in self.list(slug) if r["integrity"] == "OK"]
            if len(rows) < 2:
                return []
            keep = {rows[0]["id"]}
            state = self.manager.state(slug)
            # Never prune the checkpoint still required by the latest update/recovery.
            keep.update(
                {
                    state.get("backup_id"),
                    state.get("restore_backup"),
                    state.get("safety_backup"),
                    state.get("release_attempt", {}).get("backup_id"),
                }
            )
            now = datetime.now(UTC)
            for count, period in ((daily, "day"), (weekly, "week"), (monthly, "month")):
                buckets = set()
                for row in rows:
                    created = datetime.fromisoformat(row["created_at"])
                    key = (
                        created.date()
                        if period == "day"
                        else created.isocalendar()[:2]
                        if period == "week"
                        else (created.year, created.month)
                    )
                    if len(buckets) < count and key not in buckets:
                        keep.add(row["id"])
                        buckets.add(key)
            # Bound unreferenced safety copies, preserving all active recovery checkpoints.
            safety_rows = [r for r in rows if r["id"].startswith("pre-restore-")]
            keep.update(r["id"] for r in safety_rows[:safety])
            self.verify(slug, rows[0]["id"])
            removed = []
            for row in rows:
                if row["id"] in keep or now - datetime.fromisoformat(row["created_at"]) < timedelta(
                    days=max(1, daily)
                ):
                    continue
                path = self.directory(slug, row["id"])
                verify_files(path, slug)
                # Validated flat files only: no recursive deletion, including on Windows.
                for name in ALL_FILES:
                    (path / name).unlink()
                path.rmdir()
                removed.append(row["id"])
                self.event(slug, "retention cleanup", row["id"])
            return removed

    def recover_files(self, slug: str, backup_id: str, token: str) -> None:
        """Explicit disaster recovery of a lost registry; preserves saved identity, no clone."""
        with registry_lock(self.manager.root):
            self.verify(slug, backup_id)
            target = self.manager.directory(slug)
            if target.exists():
                raise DeploymentError("Recovery requires a missing deployment directory")
            source = self.directory(slug, backup_id)
            metadata = read_json(source / "metadata.json")
            state = metadata["deployment"]
            identity = get_bot_identity(token)
            if identity.id != state["bot_id"]:
                raise DeploymentError("Recovery token belongs to another Telegram bot")
            for existing in self.manager.list():
                if (
                    existing.get("bot_id") == identity.id
                    or existing.get("project") == state["project"]
                    or (state["domain"] and existing.get("domain") == state["domain"])
                ):
                    raise DeploymentError("Deployment identity already registered")
            if resolve_image(state["image_reference"]) != state["image_id"]:
                raise DeploymentError("Recover exact saved image using docker load/digest first")
            values = {key: secrets.token_urlsafe(36) for key in SECRET_KEYS}
            values.update(
                TELEGRAM_BOT_TOKEN=token,
                BOOKING_IMAGE=state["image_id"],
                POSTGRES_IMAGE=resolve_image("postgres:17-alpine"),
                REDIS_IMAGE=resolve_image("redis:7.4-alpine"),
            )
            private_directory(target)
            atomic_write(target / ".env", "".join(f"{k}={v}\n" for k, v in values.items()))
            atomic_write(
                target / "specialist.toml",
                (source / "specialist.toml").read_text(encoding="utf-8"),
                public_config=True,
            )
            atomic_write(
                target / "compose.yaml",
                json.dumps(
                    compose_model(state["project"], state["domain"], public=state["public"])
                ),
            )
            state["operation"] = "restore"
            state["recovery_requires_webhook"] = bool(state["public"])
            self.manager.save(slug, state, "FAILED", "recovered_files")

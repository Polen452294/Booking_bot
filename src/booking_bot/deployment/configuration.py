"""Recoverable host file + DB profile change. No service or schedule writes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from booking_bot.deployment.files import DeploymentError, atomic_write, read_json, registry_lock
from booking_bot.deployment.templates import render_specialist
from booking_bot.specialist_config import load_specialist_template

if TYPE_CHECKING:
    from booking_bot.deployment.manager import DeploymentManager


def runtime_operation(
    manager: DeploymentManager, slug: str, operation: str, snapshot: dict | None = None
) -> dict:
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
        operation,
        input_text=json.dumps(snapshot) if snapshot is not None else None,
    )
    try:
        result = json.loads(output)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except ValueError:
        raise DeploymentError(
            "Image lacks compatible configuration support; original config retained"
        ) from None


def restore_runtime(manager: DeploymentManager, slug: str, running: list[str]) -> None:
    if running:
        # Atomic host replacement changes the bind-mount inode: recreate both app containers.
        manager.compose(
            slug, "up", "-d", "--force-recreate", "--wait", "--wait-timeout", "180", "api", "worker"
        )
    else:
        manager.compose(slug, "up", "--no-start", "--force-recreate", "--no-deps", "api", "worker")
        manager.compose(slug, "stop", "redis", "postgres")


def complete(manager: DeploymentManager, slug: str, journal: dict, *, rollback: bool) -> None:
    state = dict(journal["original_state"])
    state["config_sha256"] = manager.config_digest(slug)
    manager.save(slug, state, "READY", "config_rolled_back" if rollback else "complete")


def rollback(manager: DeploymentManager, slug: str, journal: dict) -> None:
    path = manager.directory(slug)
    manager.assert_no_admin(slug)
    manager.compose(slug, "stop", "api", "worker")
    atomic_write(path / "specialist.toml", journal["previous_config"], public_config=True)
    if journal["snapshot"] is not None:
        manager.compose(slug, "up", "-d", "--wait", "--wait-timeout", "120", "postgres", "redis")
        runtime_operation(manager, slug, "restore", journal["snapshot"])
    restore_runtime(manager, slug, journal["running"])
    journal["phase"] = "rolled_back"
    atomic_write(path / "configure-journal.json", json.dumps(journal))
    complete(manager, slug, journal, rollback=True)


def configure(manager: DeploymentManager, slug: str, config: Path | None, *, resume: bool) -> None:
    from booking_bot.deployment.manager import SERVICES, validate_template

    path = manager.directory(slug)
    with registry_lock(manager.root):
        state = manager.state(slug)
        if state.get("operation") == "restore":
            raise DeploymentError("Restore interrupted; recover with bookingctl restore")
        if resume:
            if state.get("operation") != "configure":
                raise DeploymentError("No interrupted configure operation to recover")
            journal = read_json(path / "configure-journal.json")
            if journal["phase"] in {"committed", "rolled_back"}:
                complete(manager, slug, journal, rollback=journal["phase"] == "rolled_back")
            else:
                rollback(manager, slug, journal)
            return
        if state["status"] != "READY":
            raise DeploymentError("Deployment is incomplete; recover its pending operation first")
        if config is None or config.resolve() == (path / "specialist.toml").resolve():
            raise DeploymentError("Use --config with a separate candidate TOML file")
        manager.local_config(slug)
        old = load_specialist_template(path / "specialist.toml")
        new = load_specialist_template(config)
        validate_template(new)
        if new.profile.slug != slug:
            raise DeploymentError("Cannot change deployment slug")
        if new.services != old.services or new.schedule != old.schedule:
            raise DeploymentError(
                "Services/schedule are managed in Telegram; leave TOML sections unchanged"
            )
        # Currency changes would leave owner-managed service prices in another currency.
        if new.profile.currency != old.profile.currency:
            raise DeploymentError(
                "Currency changes require a separate price migration; not supported"
            )
        if state.get("config_sha256", manager.config_digest(slug)) != manager.config_digest(slug):
            raise DeploymentError(
                "Installed config was edited directly; restore the previous file first"
            )
        manager.assert_no_admin(slug)
        current = manager.runtime(slug)
        running = sorted(row["service"] for row in current if row["state"] == "running")
        if running and (
            set(running) != SERVICES or any(row["health"] != "healthy" for row in current)
        ):
            raise DeploymentError(
                "Partial/unhealthy runtime: use doctor and start before configure"
            )
        journal = {
            "phase": "prepared",
            "original_state": dict(state),
            "running": running,
            "previous_config": (path / "specialist.toml").read_text(encoding="utf-8"),
            "snapshot": None,
        }
        atomic_write(path / "configure-journal.json", json.dumps(journal))
        state["operation"] = "configure"
        manager.save(slug, state, "CREATING", "configure")
        try:
            manager.compose(slug, "stop", "api", "worker")
            manager.compose(
                slug, "up", "-d", "--wait", "--wait-timeout", "120", "postgres", "redis"
            )
            journal["snapshot"] = runtime_operation(manager, slug, "snapshot")
            atomic_write(path / "configure-journal.json", json.dumps(journal))
            atomic_write(path / "specialist.toml", render_specialist(new), public_config=True)
            runtime_operation(manager, slug, "apply")
            restore_runtime(manager, slug, running)
            journal["phase"] = "committed"
            atomic_write(path / "configure-journal.json", json.dumps(journal))
            complete(manager, slug, journal, rollback=False)
        except BaseException:
            if journal["phase"] == "committed":
                raise
            try:
                rollback(manager, slug, journal)
            except BaseException:
                manager.save(slug, state, "FAILED", "configure_recovery")
                raise DeploymentError(
                    "Configure interrupted; original config and DB snapshot preserved. "
                    "Resolve Docker/admin errors, then configure SLUG --resume"
                ) from None
            raise DeploymentError(
                "Configure failed; previous file, DB profile and runtime restored"
            ) from None

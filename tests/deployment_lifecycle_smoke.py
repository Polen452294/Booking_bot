"""Phase 3B assertions against the real installations created by smoke_deployments."""

import json
from dataclasses import replace
from unittest.mock import patch

from booking_bot.deployment.files import DeploymentError
from booking_bot.deployment.templates import render_specialist
from booking_bot.specialist_config import load_specialist_template


def lifecycle(manager, first, second):
    def sql(slug, query):
        return manager.compose(
            slug, "exec", "-T", "postgres", "psql", "-U", "booking", "-Atqc", query
        ).strip()

    def healthy(slug):
        report = manager.doctor(slug)
        assert report["ok"], report
        return report

    reports = {slug: healthy(slug) for slug in (first, second)}
    second_ids = manager.compose(second, "ps", "-q")
    second_config = (manager.directory(second) / "specialist.toml").read_bytes()
    sql(
        first,
        "UPDATE services SET name='Owner changed service', price_minor=12345, "
        "is_owner_managed=true; DELETE FROM working_rules;",
    )
    path = manager.directory(first) / "specialist.toml"
    template = load_specialist_template(path)
    candidate = manager.root.parent / (manager.root.name + "-candidate.toml")
    changed = replace(
        template,
        profile=replace(template.profile, brand_name="Updated Alice"),
        location=replace(template.location, address="New address"),
    )
    candidate.write_text(render_specialist(changed), encoding="utf-8")
    original_values = manager.values(first)
    manager.configure(first, candidate)
    assert sql(first, "SELECT name FROM businesses") == "Updated Alice"
    assert sql(first, "SELECT count(*) FROM working_rules") == "0"
    assert (
        sql(
            first,
            "SELECT count(*) FROM services WHERE name='Owner changed service' "
            "AND price_minor=12345",
        )
        == "2"
    )
    assert manager.values(first) == original_values
    healthy(first)
    assert manager.compose(second, "ps", "-q") == second_ids
    assert (manager.directory(second) / "specialist.toml").read_bytes() == second_config
    healthy(second)
    print(
        "Profile apply preserves owner services and empty schedule; second client unchanged",
        flush=True,
    )

    before = path.read_bytes()
    candidate.write_text("invalid = [", encoding="utf-8")
    try:
        manager.configure(first, candidate)
    except Exception:
        pass
    else:
        raise AssertionError("Invalid config accepted")
    assert path.read_bytes() == before
    candidate.write_text(
        render_specialist(
            replace(changed, profile=replace(changed.profile, brand_name="Must roll back"))
        ),
        encoding="utf-8",
    )
    original_compose = manager.compose
    failed = False

    def fail_health(slug, *args, **kwargs):
        nonlocal failed
        if slug == first and "--force-recreate" in args and not failed:
            failed = True
            # Real failure from Docker after the new DB profile has committed.
            return original_compose(slug, "exec", "-T", "postgres", "sh", "-c", "exit 17")
        return original_compose(slug, *args, **kwargs)

    with patch.object(manager, "compose", fail_health):
        try:
            manager.configure(first, candidate)
        except DeploymentError as error:
            assert "restored" in str(error)
        else:
            raise AssertionError("Expected Docker failure")
    assert path.read_bytes() == before
    assert sql(first, "SELECT name FROM businesses") == "Updated Alice"
    healthy(first)
    healthy(second)

    # Fail both apply and rollback transport, then recover from the persisted journal.
    def lost_connection(slug, *args, **kwargs):
        if slug == first and args[-1] in {"apply", "restore"}:
            raise DeploymentError("Simulated lost Docker connection")
        return original_compose(slug, *args, **kwargs)

    with patch.object(manager, "compose", lost_connection):
        try:
            manager.configure(first, candidate)
        except DeploymentError:
            assert manager.state(first)["status"] == "FAILED"
        else:
            raise AssertionError("Expected interrupted configure")
    assert not manager.doctor(first)["ok"]
    healthy(second)
    manager.configure(first, resume=True)
    assert sql(first, "SELECT name FROM businesses") == "Updated Alice"
    assert path.read_bytes() == before
    healthy(first)
    print(
        "Invalid config, post-commit failure rollback and interrupted configure recovery: OK",
        flush=True,
    )

    # Diagnose migration mismatch, missing worker, and unavailable Redis independently.
    revision = sql(first, "SELECT version_num FROM alembic_version")
    try:
        sql(first, "UPDATE alembic_version SET version_num='wrong_revision'")
        report = manager.doctor(first)
        assert not next(c for c in report["checks"] if c["check"] == "migrations")["ok"]
        healthy(second)
    finally:
        sql(first, f"UPDATE alembic_version SET version_num='{revision}'")
    manager.compose(first, "stop", "worker", "redis")
    report = manager.doctor(first)
    assert not report["ok"]
    for check in ("redis_ping", "worker_heartbeat", "api_readiness"):
        assert not next(c for c in report["checks"] if c["check"] == check)["ok"]
    healthy(second)
    manager.action(first, "start")
    manager.action(first, "stop")
    assert not manager.doctor(first)["ok"]
    candidate.write_text(
        render_specialist(
            replace(
                changed, profile=replace(changed.profile, brand_name="Configured while stopped")
            )
        ),
        encoding="utf-8",
    )
    manager.configure(first, candidate)
    assert all(row["state"] != "running" for row in manager.runtime(first))
    manager.action(first, "start")
    assert sql(first, "SELECT name FROM businesses") == "Configured while stopped"
    assert sql(first, "SELECT count(*) FROM working_rules") == "0"
    reports[first] = healthy(first)
    reports[second] = healthy(second)
    assert manager.compose(second, "ps", "-q") == second_ids
    for slug, report in reports.items():
        output = json.dumps(report) + manager.logs(slug, None, 100)
        for key in (
            "TELEGRAM_BOT_TOKEN",
            "POSTGRES_PASSWORD",
            "REDIS_PASSWORD",
            "TELEGRAM_WEBHOOK_HEADER_SECRET",
        ):
            assert manager.values(slug)[key] not in output
    (manager.root.parent / (manager.root.name + "-doctor.json")).write_text(
        json.dumps(reports, indent=2), encoding="utf-8"
    )
    print(
        "PASS Phase 3B: doctor, profile lifecycle, rollback/recovery, stopped config, "
        "isolation under failure, permissions, resources and secret checks",
        flush=True,
    )

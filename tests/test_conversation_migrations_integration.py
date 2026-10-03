"""Exercise real Alembic upgrades in disposable databases, never the configured schema."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from booking_bot.config import get_settings

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = "b62f3d910ea4"
HEAD = "c75a01d29f10"


async def migrate(url, target):
    environment = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)}
    image = os.environ.get(
        "BOOKING_TEST_PRECONVERSATIONS_IMAGE"
        if target == PREVIOUS
        else "BOOKING_TEST_MIGRATION_IMAGE"
    )
    command = [sys.executable, "-m", "alembic", "upgrade", target]
    if image:
        if (
            url.host not in {"127.0.0.1", "localhost"}
            or url.port != 55432
            or not url.database.startswith("phase75a_migration_")
        ):
            raise RuntimeError("Image upgrade drill requires the disposable migration database")
        docker_url = url.set(host="host.docker.internal").render_as_string(hide_password=False)
        command = [
            "docker",
            "run",
            "--rm",
            "--mount",
            f"type=bind,source={ROOT / 'specialist.toml'},target=/app/specialist.toml,readonly",
            "-e",
            f"DATABASE_URL={docker_url}",
            "--entrypoint",
            "python",
            image,
            "-m",
            "alembic",
            "upgrade",
            target,
        ]
    result = await asyncio.to_thread(
        subprocess.run,
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-3000:]


@pytest.mark.parametrize("existing", [False, True])
async def test_fresh_and_populated_previous_database_upgrade(existing):
    url = make_url(get_settings().database_url)
    # The integration profile uses a dedicated PostgreSQL superuser.
    name = "phase75a_migration_" + uuid4().hex
    admin = await asyncpg.connect(
        url.set(drivername="postgresql").render_as_string(
            hide_password=False,
        )
    )
    await admin.execute(f'CREATE DATABASE "{name}"')
    target = url.set(database=name)
    connection = None
    try:
        if existing:
            await migrate(target, PREVIOUS)
            connection = await asyncpg.connect(
                target.set(drivername="postgresql").render_as_string(
                    hide_password=False,
                )
            )
            business, service = uuid4(), uuid4()
            await connection.execute(
                "INSERT INTO businesses (id,slug,name,timezone,locale,currency,is_active) "
                "VALUES ($1,'old-version','Legacy','Europe/Moscow','ru','RUB',true)",
                business,
            )
            await connection.execute(
                "INSERT INTO services (id,business_id,name,duration_minutes,buffer_before_minutes, "
                "buffer_after_minutes,price_minor,currency,requires_approval,requires_deposit, "
                "is_owner_managed,is_active) "
                "VALUES ($1,$2,'Legacy',60,0,0,12345,'RUB',false,false, "
                "true,true)",
                service,
                business,
            )
            client, master, entry, appointment, job = [uuid4() for _ in range(5)]
            await connection.execute(
                "INSERT INTO telegram_users (id,telegram_user_id,first_name,phone) "
                "VALUES ($1,123456789,'Legacy client','+79990000000')",
                client,
            )
            await connection.execute(
                "INSERT INTO masters (id,business_id,display_name,is_active) "
                "VALUES ($1,$2,'Legacy master',true)",
                master,
                business,
            )
            await connection.execute(
                "INSERT INTO calendar_entries "
                "(id,business_id,master_id,starts_at,ends_at,kind,state) "
                "VALUES ($1,$2,$3,'2030-01-02T09:00:00Z','2030-01-02T10:00:00Z', "
                "'appointment','active')",
                entry,
                business,
                master,
            )
            await connection.execute(
                "INSERT INTO working_rules "
                "(id,business_id,master_id,weekday,start_time,end_time,is_active) "
                "VALUES ($1,$2,$3,2,'09:00','18:00',true)",
                uuid4(),
                business,
                master,
            )
            await connection.execute(
                "INSERT INTO appointments (id,business_id,calendar_entry_id,service_id,client_id, "
                "status,service_name_snapshot,service_starts_at,service_ends_at,duration_minutes, "
                "price_minor,currency,lock_version) VALUES ($1,$2,$3,$4,$5,'confirmed','Legacy', "
                "'2030-01-02T09:00:00Z','2030-01-02T10:00:00Z',60,12345,'RUB',1)",
                appointment,
                business,
                entry,
                service,
                client,
            )
            await connection.execute(
                "INSERT INTO notification_jobs (id,business_id,appointment_id,recipient_user_id, "
                "kind,scheduled_for,state,attempt_count) VALUES ($1,$2,$3,$4,'client_reminder_3d', "
                "'2030-01-01T09:00:00Z','pending',0)",
                job,
                business,
                appointment,
                client,
            )
            before = {
                table: json.loads(
                    await connection.fetchval(
                        f"SELECT row_to_json(t)::text FROM {table} t",
                    )
                )
                for table in (
                    "services",
                    "appointments",
                    "notification_jobs",
                    "telegram_users",
                    "masters",
                    "working_rules",
                    "calendar_entries",
                )
            }
        await migrate(target, "head")
        if connection is None:
            connection = await asyncpg.connect(
                target.set(drivername="postgresql").render_as_string(
                    hide_password=False,
                )
            )
        assert await connection.fetchval("SELECT version_num FROM alembic_version") == HEAD
        for table in (
            "booking_requests",
            "conversations",
            "conversation_messages",
            "conversation_read_states",
            "price_proposals",
        ):
            assert await connection.fetchval("SELECT to_regclass($1) IS NOT NULL", table)
        if existing:
            assert await connection.fetchval("SELECT pricing_mode FROM services") == "fixed"
            for table, extra_columns in {
                "services": {"pricing_mode": "fixed"},
                "appointments": {"booking_request_id": None},
                "notification_jobs": {"booking_request_id": None, "event_key": None},
                "telegram_users": {},
                "masters": {},
                "working_rules": {},
                "calendar_entries": {},
            }.items():
                after = json.loads(
                    await connection.fetchval(
                        f"SELECT row_to_json(t)::text FROM {table} t",
                    )
                )
                for column, default in extra_columns.items():
                    assert after.pop(column) == default
                assert after == before[table]
        # A second upgrade is harmless (Phase 6 retries).
        await migrate(target, "head")
    finally:
        if connection is not None:
            await connection.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()

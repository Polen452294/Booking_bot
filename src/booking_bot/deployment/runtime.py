"""Small in-image diagnostics and transactional profile operations, no Telegram calls."""

import argparse
import asyncio
import json
import sys
from importlib.metadata import version
from uuid import UUID

from alembic.config import Config
from alembic.script import ScriptDirectory
from redis.asyncio import Redis
from sqlalchemy import select, text

from booking_bot.config import get_settings
from booking_bot.db.models import Business, Location, Master
from booking_bot.db.session import async_session_factory, engine
from booking_bot.services.specialist_context import get_specialist_context
from booking_bot.services.specialist_setup import configure_specialist
from booking_bot.specialist_config import get_specialist_template
from booking_bot.version import __version__

PROFILE_FIELDS = {
    "business": (Business, ("slug", "name", "timezone", "locale", "currency", "is_active")),
    "master": (Master, ("display_name", "bio", "timezone", "is_active")),
    "location": (Location, ("name", "address", "timezone", "is_active")),
}


async def profile_rows(session):
    context = await get_specialist_context(session)
    location = await session.scalar(
        select(Location)
        .where(Location.business_id == context.business_id)
        .order_by(Location.created_at)
        .limit(1)
        .with_for_update()
    )
    if location is None:
        raise ValueError("Missing location")
    return {"business": context.business, "master": context.master, "location": location}


async def profile_operation(command: str, snapshot: dict | None = None) -> dict:
    async with async_session_factory() as session, session.begin():
        rows = await profile_rows(session)
        if command == "snapshot":
            return {
                name: {
                    "id": str(row.id),
                    "values": {field: getattr(row, field) for field in PROFILE_FIELDS[name][1]},
                }
                for name, row in rows.items()
            }
        if command == "restore":
            if snapshot is None or set(snapshot) != set(PROFILE_FIELDS):
                raise ValueError("Invalid profile snapshot")
            for name, row in rows.items():
                saved = snapshot[name]
                if saved["id"] != str(row.id) or set(saved["values"]) != set(
                    PROFILE_FIELDS[name][1]
                ):
                    raise ValueError("Snapshot belongs to another profile")
                for field, value in saved["values"].items():
                    setattr(row, field, value)
        elif command == "apply":
            await configure_specialist(session, get_specialist_template(), profile_only=True)
        else:
            raise ValueError("Unknown profile operation")
        return {"ok": True}


async def probe() -> dict:
    result = {"package_version": version("telegram-specialist-booking-bot")}
    async with async_session_factory() as session:
        try:
            async with asyncio.timeout(5):
                await session.execute(text("SELECT 1"))
            result["postgres"] = True
        except Exception:
            result["postgres"] = False
            await session.rollback()
        try:
            async with asyncio.timeout(5):
                current = list(
                    (
                        await session.execute(text("SELECT version_num FROM alembic_version"))
                    ).scalars()
                )
                heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
            result["migrations"] = {
                "ok": len(current) == len(heads) == 1 and current == heads,
                "current": current,
                "expected": heads,
            }
        except Exception:
            result["migrations"] = {"ok": False, "detail": "Cannot read Alembic revision"}
            await session.rollback()
        try:
            async with asyncio.timeout(5):
                context = await get_specialist_context(session)
                result["profile"] = context.business.slug == get_specialist_template().profile.slug
                from booking_bot.services.notification_operations import queue_counts

                result["notifications"] = await queue_counts(session, context.business_id)
                result["database_size_bytes"] = await session.scalar(
                    text("SELECT pg_database_size(current_database())")
                )
        except Exception:
            result["profile"] = False
    redis = Redis.from_url(get_settings().redis_url, socket_timeout=4, socket_connect_timeout=4)
    try:
        async with asyncio.timeout(5):
            result["redis"] = await redis.ping()
    except Exception:
        result["redis"] = False
    finally:
        await redis.aclose()
    return result


async def execute(command: str, job_id: str | None = None) -> dict:
    try:
        if command == "doctor":
            return await probe()
        if command.startswith("notifications-"):
            from booking_bot.services.notification_operations import failed_jobs, retry_jobs

            async with asyncio.timeout(10), async_session_factory() as session, session.begin():
                await session.execute(text("SET LOCAL statement_timeout = '5s'"))
                context = await get_specialist_context(session)
                if context.business.slug != get_specialist_template().profile.slug:
                    raise ValueError("Deployment identity mismatch")
                if command == "notifications-failed":
                    return {"jobs": await failed_jobs(session, context.business_id), "limit": 100}
                return await retry_jobs(
                    session, context.business_id, UUID(job_id) if job_id else None
                )
        if command == "release-check":
            scripts = ScriptDirectory.from_config(Config("alembic.ini"))
            heads = scripts.get_heads()
            async with async_session_factory() as session:
                current = list(
                    (
                        await session.execute(text("SELECT version_num FROM alembic_version"))
                    ).scalars()
                )
            ancestors = {r.revision for r in scripts.walk_revisions()}
            return {
                "version": __version__,
                "heads": heads,
                "current": current,
                "forward": len(heads) == len(current) == 1 and current[0] in ancestors,
            }
        snapshot = json.load(sys.stdin) if command == "restore" else None
        return await profile_operation(command, snapshot)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "doctor",
            "snapshot",
            "apply",
            "restore",
            "release-check",
            "notifications-failed",
            "notifications-retry",
            "notifications-retry-failed",
        ),
    )
    parser.add_argument("--job-id")
    args = parser.parse_args()
    try:
        if args.command == "notifications-retry" and not args.job_id:
            raise ValueError("Single retry requires a job ID")
        print(json.dumps(asyncio.run(execute(args.command, args.job_id))))
    except Exception:
        print("Deployment runtime operation failed; no profile changes committed", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

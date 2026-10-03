"""Optional real E2E dump/restore in an explicitly named disposable test container."""

import asyncio
import os
import subprocess
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from booking_bot.config import get_settings
from booking_bot.db.session import async_session_factory

TABLES = (
    "booking_requests",
    "conversations",
    "conversation_messages",
    "conversation_read_states",
    "price_proposals",
    "appointments",
)


def docker(container, *command, content=None):
    return subprocess.run(
        ["docker", "exec", "-i", container, *command],
        input=content,
        capture_output=True,
        check=True,
        timeout=120,
    ).stdout


async def verify_e2e_dump_restore(business_id):
    container = os.environ.get("BOOKING_TEST_POSTGRES_CONTAINER")
    if not container:
        return  # Normal integration jobs do not assume a Docker CLI/container name.
    url = make_url(get_settings().database_url)
    if (
        not container.startswith(("booking-phase75b-", "booking-phase75c-"))
        or url.database != "booking"
        or url.host not in {"127.0.0.1", "localhost"}
        or url.port != 55432
    ):
        raise RuntimeError("Dump/restore acceptance requires the dedicated Phase 7.5B test DB")
    database = f"phase75b_restore_{uuid4().hex}"
    statements = {
        table: text(
            f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY "
            f"to_jsonb(t)::text), '[]'::jsonb) FROM {table} t"
        )
        for table in TABLES
    }
    async with async_session_factory() as session:
        expected = {table: await session.scalar(query) for table, query in statements.items()}
        assert any(
            row["business_id"] == str(business_id) and row["status"] == "booked"
            for row in expected["booking_requests"]
        )
    await asyncio.to_thread(
        docker, container, "createdb", "-U", "booking", "-T", "template0", database
    )
    restored = create_async_engine(url.set(database=database))
    try:
        dump = await asyncio.to_thread(
            docker, container, "pg_dump", "-U", "booking", "-Fc", "booking"
        )
        await asyncio.to_thread(
            docker,
            container,
            "pg_restore",
            "-U",
            "booking",
            "-d",
            database,
            "--no-owner",
            "--exit-on-error",
            content=dump,
        )
        async with restored.connect() as connection:
            actual = {table: await connection.scalar(query) for table, query in statements.items()}
        assert actual == expected
        # Existing Telegram buttons are validated against the restored domain,
        # including the superseded proposal and the already booked request.
        import pytest

        from booking_bot.domain.conversations import ProposalNotPendingError
        from booking_bot.services.price_proposals import PriceProposalService

        restored_sessions = async_sessionmaker(restored, expire_on_commit=False)
        async with restored_sessions() as session, session.begin():
            for request in expected["booking_requests"]:
                if request["business_id"] != str(business_id) or request["status"] != "booked":
                    continue
                for proposal in expected["price_proposals"]:
                    if proposal["booking_request_id"] == request["id"]:
                        with pytest.raises(ProposalNotPendingError):
                            await PriceProposalService().accept(
                                session,
                                business_id=business_id,
                                request_id=UUID(request["id"]),
                                actor_user_id=UUID(request["client_user_id"]),
                                proposal_id=UUID(proposal["id"]),
                            )
        async with restored.connect() as connection:
            assert {
                table: await connection.scalar(query) for table, query in statements.items()
            } == expected
    finally:
        await restored.dispose()
        # The target is the fresh UUID database created above, never the source database.
        assert database.startswith("phase75b_restore_") and database != url.database
        await asyncio.to_thread(docker, container, "dropdb", "-U", "booking", "--force", database)

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from booking_bot.db.models import (
    Appointment,
    Business,
    CalendarEntry,
    Master,
    NotificationJob,
    Service,
    TelegramUser,
)
from booking_bot.db.session import async_session_factory
from booking_bot.services.notification_operations import failed_jobs, queue_counts, retry_jobs

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(loop_scope="session")
async def jobs():
    now = datetime.now(UTC)
    async with async_session_factory() as session, session.begin():
        business = Business(slug="ops-" + uuid4().hex, name="Operational test")
        other = Business(slug="ops-other-" + uuid4().hex, name="Other client")
        user = TelegramUser(telegram_user_id=-(uuid4().int % 2_000_000_000))
        session.add_all([business, other, user])
        await session.flush()
        master = Master(business_id=business.id, display_name="Test", timezone="UTC")
        service = Service(
            business_id=business.id, name="Test", duration_minutes=60, price_minor=100
        )
        session.add_all([master, service])
        await session.flush()
        start = now + timedelta(days=10)
        entry = CalendarEntry(
            business_id=business.id,
            master_id=master.id,
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            kind="appointment",
            state="active",
        )
        session.add(entry)
        await session.flush()
        appointment = Appointment(
            business_id=business.id,
            calendar_entry_id=entry.id,
            service_id=service.id,
            client_id=user.id,
            service_name_snapshot="Test",
            service_starts_at=start,
            service_ends_at=entry.ends_at,
            duration_minutes=60,
            price_minor=100,
            status="confirmed",
        )
        session.add(appointment)
        await session.flush()
        items = []
        for owner, error in (
            (business.id, "TelegramNetworkError"),
            (business.id, "TelegramForbiddenError"),
            (business.id, "phone +79991234567 private payload"),
            (other.id, "TelegramNetworkError"),
        ):
            job = NotificationJob(
                business_id=owner,
                appointment_id=appointment.id,
                recipient_user_id=user.id,
                kind="master_new_appointment",
                scheduled_for=now - timedelta(hours=len(items)),
                state="failed",
                attempt_count=5,
                last_error=error,
            )
            session.add(job)
            items.append(job)
        await session.flush()
        data = SimpleNamespace(
            business_id=business.id,
            other_id=other.id,
            user_id=user.id,
            appointment_id=appointment.id,
            ids=[job.id for job in items],
        )
    yield data
    async with async_session_factory() as session, session.begin():
        # Other's cross-scope negative fixture references this appointment; remove it first.
        await session.execute(delete(NotificationJob).where(NotificationJob.id.in_(data.ids)))
        await session.execute(
            delete(Business).where(Business.id.in_([data.business_id, data.other_id]))
        )
        await session.execute(delete(TelegramUser).where(TelegramUser.id == data.user_id))


async def test_failed_listing_is_scoped_bounded_and_redacted(jobs):
    async with async_session_factory() as session:
        rows = await failed_jobs(session, jobs.business_id)
        assert len(rows) == 3
        assert all(row["job_id"] != str(jobs.ids[3]) for row in rows)
        assert "+79991234567" not in str(rows) and "private payload" not in str(rows)
        assert await queue_counts(session, jobs.business_id) == {
            "pending": 0,
            "processing": 0,
            "failed": 3,
        }


async def test_retry_resets_only_explicit_transient_failed_jobs(jobs):
    async with async_session_factory() as session, session.begin():
        result = await retry_jobs(session, jobs.business_id)
        assert result["retried"] == [str(jobs.ids[0])]
    async with async_session_factory() as session:
        jobs_by_id = {
            job.id: job
            for job in await session.scalars(
                select(NotificationJob).where(NotificationJob.id.in_(jobs.ids))
            )
        }
        assert jobs_by_id[jobs.ids[0]].state == "pending"
        assert jobs_by_id[jobs.ids[0]].attempt_count == 0
        assert jobs_by_id[jobs.ids[0]].claim_token is None
        assert all(jobs_by_id[job_id].state == "failed" for job_id in jobs.ids[1:])


@pytest.mark.parametrize("index", [1, 2, 3])
async def test_single_retry_rejects_permanent_unknown_and_foreign_jobs(jobs, index):
    async with async_session_factory() as session, session.begin():
        with pytest.raises(ValueError, match="eligible"):
            await retry_jobs(session, jobs.business_id, jobs.ids[index])


async def test_retry_rejects_cancelled_booking(jobs):
    async with async_session_factory() as session, session.begin():
        appointment = await session.get(Appointment, jobs.appointment_id)
        appointment.status = "cancelled_by_client"
        await session.flush()
        result = await retry_jobs(session, jobs.business_id)
        assert not result["retried"] and result["refused"] == [str(jobs.ids[0])]


async def test_concurrent_retry_does_not_reset_claimed_or_locked_job(jobs):
    async with async_session_factory() as first, first.begin():
        assert (await retry_jobs(first, jobs.business_id))["retried"] == [str(jobs.ids[0])]
        async with async_session_factory() as second, second.begin():
            assert not (await retry_jobs(second, jobs.business_id))["retried"]

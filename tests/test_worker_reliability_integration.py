import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendMessage
from redis.asyncio import Redis
from sqlalchemy import delete

from booking_bot.config import Settings, get_settings
from booking_bot.db.models import Business, NotificationJob, TelegramUser
from booking_bot.db.session import async_session_factory
from booking_bot.services.notification_delivery import DeliveryPayload, NotificationDeliveryService
from booking_bot.services.worker_health import (
    HEARTBEAT_TTL,
    WorkerHeartbeat,
    check_worker_health,
)

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(loop_scope="session")
async def queue():
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        business = Business(slug=f"worker-{uuid4().hex}", name="Worker test")
        user = TelegramUser(telegram_user_id=-(uuid4().int % 2_000_000_000))
        session.add_all([business, user])
        await session.flush()
        job = NotificationJob(
            business_id=business.id,
            recipient_user_id=user.id,
            kind="test",
            scheduled_for=now - timedelta(seconds=1),
        )
        session.add(job)
        await session.commit()
    settings = Settings(_env_file=None)

    def worker():
        service = NotificationDeliveryService(settings)
        service._build_payload = AsyncMock(return_value=DeliveryPayload(chat_id=123, text="test"))
        return service

    async def read():
        async with async_session_factory() as session:
            return await session.get(NotificationJob, job.id)

    yield SimpleNamespace(
        business_id=business.id,
        job_id=job.id,
        user_id=user.id,
        worker=worker,
        now=now,
        read=read,
    )
    async with async_session_factory() as session:
        await session.execute(delete(Business).where(Business.id == business.id))
        await session.execute(delete(TelegramUser).where(TelegramUser.id == user.id))
        await session.commit()


@pytest.mark.parametrize(
    "error,state,delay",
    [
        (None, "sent", None),
        (TelegramNetworkError, "pending", 15),
        (TelegramServerError, "pending", 15),
        (TelegramRetryAfter, "pending", 120),
        (TelegramForbiddenError, "failed", None),
        (TelegramBadRequest, "failed", None),
    ],
)
async def test_delivery_outcomes(queue, error, state, delay):
    bot = AsyncMock()
    if error:
        kwargs = {"retry_after": 120} if error is TelegramRetryAfter else {}
        bot.send_message.side_effect = error(
            method=SendMessage(chat_id=123, text="test"), message="test failure", **kwargs
        )
    before = datetime.now(UTC)
    worker = queue.worker()
    assert await worker.run_once(bot, business_id=queue.business_id) == 1
    job = await queue.read()
    assert job.state == state
    assert job.attempt_count == 1
    assert job.claim_token is None
    assert (job.sent_at is not None) == (state == "sent")
    if delay:
        assert job.scheduled_for >= before + timedelta(seconds=delay)
        # Retry is scheduled in the future, never a hot loop.
        assert await worker.run_once(bot, business_id=queue.business_id) == 0
    else:
        assert await worker.run_once(bot, business_id=queue.business_id) == 0
    bot.send_message.assert_awaited_once()


@pytest.mark.parametrize("state,attempts", [("pending", 5), ("processing", 5), ("pending", 4)])
async def test_max_attempts(queue, state, attempts):
    async with async_session_factory() as session:
        job = await session.get(NotificationJob, queue.job_id)
        job.state, job.attempt_count = state, attempts
        job.updated_at = queue.now - timedelta(minutes=6)
        await session.commit()
    bot = AsyncMock()
    bot.send_message.side_effect = TimeoutError()
    await queue.worker().run_once(bot, business_id=queue.business_id)
    job = await queue.read()
    assert job.state == "failed"
    assert job.attempt_count == 5
    assert bot.send_message.await_count == (1 if attempts == 4 else 0)


async def test_unsupported_notification_is_permanent(queue):
    bot = AsyncMock()
    # Real payload builder rejects incomplete/unsupported notification context.
    worker = NotificationDeliveryService(Settings(_env_file=None))
    await worker.run_once(bot, business_id=queue.business_id)
    assert (await queue.read()).state == "failed"
    bot.send_message.assert_not_awaited()


async def test_disabled_notification_is_cancelled(queue):
    bot, worker = AsyncMock(), queue.worker()
    worker._is_enabled = AsyncMock(return_value=False)
    await worker.run_once(bot, business_id=queue.business_id)
    assert (await queue.read()).state == "cancelled"
    bot.send_message.assert_not_awaited()


async def test_stale_recovery_fences_former_owner(queue):
    old, new = queue.worker(), queue.worker()
    assert await old._claim_jobs(business_id=queue.business_id, now=queue.now) == [queue.job_id]
    future = queue.now + timedelta(minutes=6)
    assert await new._claim_jobs(business_id=queue.business_id, now=future) == [queue.job_id]
    await old._release_jobs([queue.job_id])
    assert (await queue.read()).state == "processing"
    bot = AsyncMock()
    await old._deliver_job(bot, job_id=queue.job_id, now=future)
    bot.send_message.assert_not_awaited()
    await new._deliver_job(bot, job_id=queue.job_id, now=future)
    bot.send_message.assert_awaited_once()
    assert (await queue.read()).attempt_count == 2


async def test_parallel_workers_claim_disjoint_batches(queue):
    async with async_session_factory() as session:
        session.add_all(
            [
                NotificationJob(
                    business_id=queue.business_id,
                    recipient_user_id=queue.user_id,
                    kind="test",
                    scheduled_for=queue.now - timedelta(seconds=i + 2),
                )
                for i in range(7)
            ]
        )
        await session.commit()
    settings = Settings(_env_file=None, notification_batch_size=4)
    first, second = NotificationDeliveryService(settings), NotificationDeliveryService(settings)
    a, b = await asyncio.gather(
        first._claim_jobs(business_id=queue.business_id, now=queue.now),
        second._claim_jobs(business_id=queue.business_id, now=queue.now),
    )
    assert len(a) == len(b) == 4
    assert not set(a) & set(b)


async def test_live_send_is_not_recovered_even_if_stale(queue):
    first, second = queue.worker(), queue.worker()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def send(**kwargs):
        entered.set()
        await finish.wait()

    bot = AsyncMock()
    bot.send_message.side_effect = send
    task = asyncio.create_task(first.run_once(bot, business_id=queue.business_id, now=queue.now))
    await asyncio.wait_for(entered.wait(), 3)
    try:
        assert (
            await second.run_once(
                bot, business_id=queue.business_id, now=queue.now + timedelta(minutes=6)
            )
            == 0
        )
    finally:
        finish.set()
        await task
    bot.send_message.assert_awaited_once()
    assert (await queue.read()).state == "sent"


async def test_heartbeat_health_is_per_worker_and_expires(capsys):
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    worker_id = f"test-{uuid4().hex}"
    heartbeat = WorkerHeartbeat(redis, settings, worker_id)
    try:
        await heartbeat.beat()
        assert 0 < await redis.ttl(heartbeat.key) <= HEARTBEAT_TTL
        assert await check_worker_health(settings, worker_id)
        assert not await check_worker_health(settings, worker_id + "-other")
        await redis.set(
            heartbeat.key,
            (datetime.now(UTC) - timedelta(seconds=HEARTBEAT_TTL + 1)).isoformat(),
            ex=HEARTBEAT_TTL,
        )
        assert not await check_worker_health(settings, worker_id)
        await heartbeat.clear()
        assert not await check_worker_health(settings, worker_id)
        assert "Worker: UNHEALTHY" in capsys.readouterr().out
    finally:
        await heartbeat.clear()
        await redis.aclose()

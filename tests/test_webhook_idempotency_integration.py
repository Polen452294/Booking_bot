import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import delete, select

from booking_bot.config import Settings, get_settings
from booking_bot.db.models import Business, TelegramUpdateReceipt
from booking_bot.db.session import async_session_factory
from booking_bot.services import telegram_webhook as webhook
from booking_bot.services.update_idempotency import (
    PROCESSED_TTL,
    PROCESSING_TTL,
    UpdateInProgressError,
    UpdateLease,
    claim_receipt,
)

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(loop_scope="session")
async def delivery(monkeypatch):
    bot_id = uuid4().int % 10**12
    settings = Settings(
        _env_file=None,
        telegram_bot_token=f"{bot_id}:test-token",
        telegram_webhook_header_secret="secret",
    )
    redis = Redis.from_url(get_settings().redis_url)
    monkeypatch.setattr(webhook.dispatcher.storage, "redis", redis)
    monkeypatch.setattr(webhook, "create_telegram_bot", lambda *_: AsyncMock())
    monkeypatch.setattr(
        webhook,
        "get_specialist_context",
        AsyncMock(return_value=SimpleNamespace(business_id=uuid4(), master_id=uuid4())),
    )
    slug = f"webhook-{uuid4().hex}"

    async def handler(*args, db_session, **kwargs):
        db_session.add(Business(slug=slug, name="Webhook test"))
        await db_session.flush()

    feed = AsyncMock(side_effect=handler)

    async def dispatch(*args, commit_update, **kwargs):
        await feed(*args, **kwargs)
        await commit_update()

    monkeypatch.setattr(webhook.dispatcher, "feed_update", dispatch)

    async def process(update_id=101):
        async with async_session_factory() as session:
            await webhook.TelegramWebhookService(settings).process(
                webhook_header_secret="secret",
                payload={
                    "update_id": update_id,
                    "message": {
                        "message_id": 1,
                        "date": 1700000000,
                        "chat": {"id": 1, "type": "private"},
                    },
                },
                session=session,
            )

    yield SimpleNamespace(
        settings=settings,
        redis=redis,
        process=process,
        feed=feed,
        handler=handler,
        slug=slug,
    )
    keys = [key async for key in redis.scan_iter(f"{settings.redis_namespace}:*")]
    if keys:
        await redis.delete(*keys)
    await redis.aclose()
    async with async_session_factory() as session:
        await session.execute(
            delete(TelegramUpdateReceipt).where(
                TelegramUpdateReceipt.namespace == settings.redis_namespace
            )
        )
        await session.execute(delete(Business).where(Business.slug == slug))
        await session.commit()


async def test_concurrent_duplicate_executes_business_once(delivery):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def handler(*args, **kwargs):
        await delivery.handler(*args, **kwargs)
        entered.set()
        await finish.wait()

    delivery.feed.side_effect = handler
    first = asyncio.create_task(delivery.process())
    await asyncio.wait_for(entered.wait(), 3)
    try:
        with pytest.raises(UpdateInProgressError):
            await delivery.process()
    finally:
        finish.set()
        await first
    await delivery.process()
    delivery.feed.assert_awaited_once()
    key = UpdateLease(delivery.redis, delivery.settings.redis_namespace, 101).key
    assert await delivery.redis.get(key) == b"processed"
    assert 0 < await delivery.redis.ttl(key) <= PROCESSED_TTL
    async with async_session_factory() as session:
        assert await session.scalar(select(Business.id).where(Business.slug == delivery.slug))


async def test_failure_rolls_back_receipt_and_allows_retry(delivery):
    async def failing(*args, **kwargs):
        await delivery.handler(*args, **kwargs)
        raise RuntimeError("temporary")

    delivery.feed.side_effect = failing
    with pytest.raises(RuntimeError):
        await delivery.process()
    delivery.feed.side_effect = delivery.handler
    await delivery.process()
    assert delivery.feed.await_count == 2


async def test_commit_then_redis_failure_does_not_reexecute(delivery, monkeypatch):
    original = UpdateLease.finish
    monkeypatch.setattr(UpdateLease, "finish", AsyncMock(side_effect=RedisConnectionError()))
    with pytest.raises(RedisConnectionError):
        await delivery.process()
    monkeypatch.setattr(UpdateLease, "finish", original)
    await delivery.process()
    delivery.feed.assert_awaited_once()


async def test_redis_loss_still_serializes_database_receipt(delivery):
    namespace = delivery.settings.redis_namespace
    async with async_session_factory() as first:
        assert await claim_receipt(first, namespace, 102)
        started = asyncio.Event()

        async def other():
            async with async_session_factory() as second:
                started.set()
                claimed = await claim_receipt(second, namespace, 102)
                await second.commit()
                return claimed

        task = asyncio.create_task(other())
        await started.wait()
        await first.commit()
        assert not await task


async def test_lease_expiry_and_old_owner_cannot_delete_new_lease(delivery):
    old = UpdateLease(delivery.redis, delivery.settings.redis_namespace, 103)
    assert await old.acquire()
    assert 0 < await delivery.redis.ttl(old.key) <= PROCESSING_TTL
    await delivery.redis.pexpire(old.key, 1)
    await asyncio.sleep(0.02)
    new = UpdateLease(delivery.redis, delivery.settings.redis_namespace, 103)
    assert await new.acquire()
    await old.release()
    with pytest.raises(UpdateInProgressError):
        await old.finish()
    assert await delivery.redis.get(new.key) == new.token.encode()

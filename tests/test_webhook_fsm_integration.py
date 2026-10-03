from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import DefaultKeyBuilder, StorageKey
from aiogram.fsm.storage.redis import RedisStorage
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from booking_bot.bot.middlewares import DatabaseSessionMiddleware
from booking_bot.config import Settings, get_settings
from booking_bot.db.models import Business, TelegramUpdateReceipt
from booking_bot.db.session import async_session_factory
from booking_bot.services import telegram_webhook as webhook

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("failure", ["handler", "commit"])
async def test_failed_webhook_preserves_fsm_for_successful_retry(monkeypatch, failure):
    settings = Settings(
        _env_file=None,
        telegram_bot_token=f"{uuid4().int % 10**12}:test-token",
        telegram_webhook_header_secret="secret",
    )
    prefix = f"fsm-test:{uuid4().hex}"
    storage = RedisStorage.from_url(
        get_settings().redis_url,
        key_builder=DefaultKeyBuilder(prefix=prefix, with_bot_id=True),
        state_ttl=120,
        data_ttl=120,
    )
    dispatcher = Dispatcher(
        storage=storage,
        events_isolation=storage.create_isolation(lock_kwargs={"timeout": 120}),
    )
    dispatcher.update.outer_middleware(DatabaseSessionMiddleware())
    router = Router()
    fail_handler = failure == "handler"
    calls = 0
    slug = f"fsm-retry-{uuid4().hex}"
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    original = FSMContext(storage, StorageKey(bot_id=bot.id, chat_id=101, user_id=101))

    @router.message()
    async def handler(message, state, db_session):
        nonlocal calls
        calls += 1
        assert await state.get_state() == "confirming"
        assert await state.get_value("hold_id") == "held-slot"
        await state.update_data(extra="buffered")
        assert (await state.get_data())["extra"] == "buffered"
        db_session.add(Business(slug=slug, name="FSM retry test"))
        await db_session.flush()
        await state.clear()
        # Redis remains unchanged until PostgreSQL confirms the transaction.
        assert await original.get_state() == "confirming"
        assert await original.get_data() == {"hold_id": "held-slot"}
        if fail_handler:
            raise RuntimeError("temporary Telegram error after state.clear")

    dispatcher.include_router(router)
    monkeypatch.setattr(webhook, "dispatcher", dispatcher)
    monkeypatch.setattr(webhook, "create_telegram_bot", lambda *_: bot)
    monkeypatch.setattr(
        webhook,
        "get_specialist_context",
        AsyncMock(
            return_value=SimpleNamespace(business_id=uuid4(), master_id=uuid4()),
        ),
    )
    payload = {
        "update_id": 123,
        "message": {
            "message_id": 1,
            "date": 1700000000,
            "chat": {"id": 101, "type": "private"},
            "from": {"id": 101, "is_bot": False, "first_name": "Test"},
            "text": "confirm",
        },
    }
    service = webhook.TelegramWebhookService(settings)
    try:
        await original.set_state("confirming")
        await original.set_data({"hold_id": "held-slot"})
        async with async_session_factory() as session:
            if failure == "commit":
                session.commit = AsyncMock(side_effect=OperationalError("COMMIT", {}, Exception()))
            with pytest.raises((RuntimeError, OperationalError)):
                await service.process(
                    webhook_header_secret="secret", payload=payload, session=session
                )
        assert await original.get_state() == "confirming"
        assert await original.get_data() == {"hold_id": "held-slot"}
        async with async_session_factory() as session:
            assert await session.scalar(select(Business.id).where(Business.slug == slug)) is None
            assert await session.get(TelegramUpdateReceipt, (settings.redis_namespace, 123)) is None
        fail_handler = False
        async with async_session_factory() as session:
            await service.process(webhook_header_secret="secret", payload=payload, session=session)
        assert await original.get_state() is None
        assert await original.get_data() == {}
        async with async_session_factory() as session:
            await service.process(webhook_header_secret="secret", payload=payload, session=session)
            assert (
                await session.scalar(select(Business.id).where(Business.slug == slug)) is not None
            )
        assert calls == 2
    finally:
        keys = [key async for key in storage.redis.scan_iter(f"{prefix}:*")]
        keys.extend([key async for key in storage.redis.scan_iter(f"{settings.redis_namespace}:*")])
        if keys:
            await storage.redis.delete(*keys)
        await dispatcher.fsm.close()
        await bot.session.close()
        async with async_session_factory() as session:
            await session.execute(delete(Business).where(Business.slug == slug))
            await session.execute(
                delete(TelegramUpdateReceipt).where(
                    TelegramUpdateReceipt.namespace == settings.redis_namespace,
                )
            )
            await session.commit()

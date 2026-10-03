import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from booking_bot.config import Settings
from booking_bot.db.models import Business, NotificationJob, TelegramUser
from booking_bot.db.session import async_session_factory
from booking_bot.services.notification_delivery import DeliveryPayload, NotificationDeliveryService

pytestmark = pytest.mark.integration


async def test_shutdown_persists_sent_job_and_releases_remaining_claim(monkeypatch) -> None:
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        business = Business(slug=f"shutdown-{uuid4().hex[:12]}", name="Shutdown test")
        user = TelegramUser(telegram_user_id=-(uuid4().int % 2_000_000_000))
        session.add_all([business, user])
        await session.flush()
        jobs = [
            NotificationJob(
                business_id=business.id,
                recipient_user_id=user.id,
                kind="test",
                scheduled_for=now - timedelta(seconds=10 - index),
            )
            for index in range(2)
        ]
        session.add_all(jobs)
        await session.commit()
        business_id, user_id = business.id, user.id
    try:
        stop = asyncio.Event()
        bot = AsyncMock()

        async def send(**kwargs):
            stop.set()

        bot.send_message.side_effect = send
        service = NotificationDeliveryService(Settings(_env_file=None))
        monkeypatch.setattr(
            service,
            "_build_payload",
            AsyncMock(
                return_value=DeliveryPayload(chat_id=123, text="Test notification"),
            ),
        )
        assert await service.run_once(bot, business_id=business_id, now=now, stop=stop) == 1
        async with async_session_factory() as session:
            result = list(
                (
                    await session.scalars(
                        select(NotificationJob)
                        .where(NotificationJob.business_id == business_id)
                        .order_by(NotificationJob.scheduled_for)
                    )
                ).all()
            )
            assert [(job.state, job.attempt_count) for job in result] == [
                ("sent", 1),
                ("pending", 0),
            ]
            assert result[0].sent_at is not None
        bot.send_message.assert_awaited_once()
    finally:
        async with async_session_factory() as session:
            await session.execute(delete(Business).where(Business.id == business_id))
            await session.execute(delete(TelegramUser).where(TelegramUser.id == user_id))
            await session.commit()

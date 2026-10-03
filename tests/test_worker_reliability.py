import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from booking_bot.config import Settings
from booking_bot.services.notification_delivery import (
    NotificationDeliveryService,
    UnsupportedNotificationError,
)


async def test_worker_does_not_claim_after_stop():
    worker = NotificationDeliveryService(Settings(_env_file=None))
    worker._claim_jobs = AsyncMock()
    stop = asyncio.Event()
    stop.set()
    assert await worker.run_once(AsyncMock(), business_id=uuid4(), stop=stop) == 0
    worker._claim_jobs.assert_not_awaited()


async def test_heartbeat_does_not_advance_during_stuck_send():
    heartbeat = AsyncMock()
    worker = NotificationDeliveryService(Settings(_env_file=None), heartbeat)
    worker._claim_jobs = AsyncMock(return_value=[uuid4()])
    entered, finish, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def deliver(*args, **kwargs):
        entered.set()
        await finish.wait()

    worker._deliver_job = AsyncMock(side_effect=deliver)
    task = asyncio.create_task(worker.run_forever(AsyncMock(), business_id=uuid4(), stop=stop))
    await entered.wait()
    beats = heartbeat.beat.await_count
    stop.set()
    await asyncio.sleep(0)
    assert heartbeat.beat.await_count == beats
    assert not task.done()
    finish.set()
    await asyncio.wait_for(task, 1)
    assert heartbeat.beat.await_count > beats


@pytest.mark.parametrize("kind", ["unsupported", "client_reminder_unknown"])
async def test_unknown_kind_rejected_even_with_complete_context(kind):
    worker = NotificationDeliveryService(Settings(_env_file=None))
    session = AsyncMock()
    session.get.side_effect = [
        SimpleNamespace(telegram_user_id=123),
        SimpleNamespace(timezone="UTC"),
        SimpleNamespace(calendar_entry_id=uuid4(), service_starts_at=datetime.now(UTC)),
        SimpleNamespace(master_id=uuid4(), location_id=None),
        SimpleNamespace(timezone="UTC"),
    ]
    job = SimpleNamespace(
        recipient_user_id=uuid4(),
        business_id=uuid4(),
        appointment_id=uuid4(),
        kind=kind,
    )
    with pytest.raises(UnsupportedNotificationError):
        await worker._build_payload(session, job)

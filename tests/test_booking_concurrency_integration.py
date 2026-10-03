import asyncio
from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from booking_bot.config import Settings
from booking_bot.db.models import (
    Appointment,
    Business,
    CalendarEntry,
    Master,
    MasterService,
    Service,
    SlotHold,
    TelegramUser,
    WorkingRule,
)
from booking_bot.db.session import async_session_factory
from booking_bot.services.bookings import BookingService, HoldExpiredError, SlotUnavailableError

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(loop_scope="session")
async def booking_data():
    now = datetime(2026, 10, 1, 6, tzinfo=UTC)
    day = date(2026, 10, 5)
    start = datetime(2026, 10, 5, 12, tzinfo=UTC)  # 15:00 Moscow
    async with async_session_factory() as session:
        business = Business(
            slug=f"race-{uuid4().hex}", name="Concurrency", timezone="Europe/Moscow"
        )
        clients = [
            TelegramUser(
                telegram_user_id=-(uuid4().int % 2_000_000_000),
                first_name="Test",
                phone="+79990000000",
            )
            for _ in range(2)
        ]
        session.add_all([business, *clients])
        await session.flush()
        master = Master(business_id=business.id, display_name="Test", timezone="Europe/Moscow")
        service = Service(business_id=business.id, name="Test", duration_minutes=60)
        session.add_all([master, service])
        await session.flush()
        session.add_all(
            [
                MasterService(business_id=business.id, master_id=master.id, service_id=service.id),
                WorkingRule(
                    business_id=business.id,
                    master_id=master.id,
                    weekday=day.weekday(),
                    start_time=time(15),
                    end_time=time(17),
                ),
            ]
        )
        await session.commit()
    booking = BookingService(Settings(_env_file=None))

    async def hold(session, client_id, service_start=start):
        return await booking.create_hold(
            session,
            business_id=business.id,
            master_id=master.id,
            service_id=service.id,
            client_id=client_id,
            service_start=service_start,
            local_date=day,
            now=now,
        )

    yield SimpleNamespace(
        business_id=business.id,
        clients=[c.id for c in clients],
        booking=booking,
        hold=hold,
        now=now,
        start=start,
    )
    async with async_session_factory() as session:
        await session.execute(delete(Business).where(Business.id == business.id))
        await session.execute(
            delete(TelegramUser).where(TelegramUser.id.in_([c.id for c in clients]))
        )
        await session.commit()


async def assert_one_active(data):
    async with async_session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Appointment)
                .where(
                    Appointment.business_id == data.business_id,
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CalendarEntry)
                .where(
                    CalendarEntry.business_id == data.business_id,
                    CalendarEntry.kind == "appointment",
                    CalendarEntry.state == "active",
                )
            )
            == 1
        )


async def test_double_confirm_does_not_expire_converted_appointment(booking_data):
    data = booking_data
    async with async_session_factory() as session:
        hold = await data.hold(session, data.clients[0])
        await session.commit()
    start = asyncio.Event()

    async def confirm():
        await start.wait()
        async with async_session_factory() as session:
            try:
                await data.booking.confirm_hold(
                    session,
                    hold_id=hold.id,
                    client_id=data.clients[0],
                    now=data.now,
                )
                result = "confirmed"
            except HoldExpiredError:
                result = "already_used"
            # Handlers catch business errors, then the webhook commits the transaction.
            await session.commit()
            return result

    tasks = [asyncio.create_task(confirm()) for _ in range(2)]
    start.set()
    assert sorted(await asyncio.wait_for(asyncio.gather(*tasks), 5)) == [
        "already_used",
        "confirmed",
    ]
    await assert_one_active(data)
    async with async_session_factory() as session:
        assert (await session.get(SlotHold, hold.id)).status == "converted"


async def test_two_clients_compete_for_same_slot(booking_data, monkeypatch):
    data = booking_data
    # Both clients see the free slot before either writes; exercise the real PG exclusion.
    original = data.booking._availability.list_slots
    barrier = asyncio.Barrier(2)

    async def list_slots(*args, **kwargs):
        slots = await original(*args, **kwargs)
        await barrier.wait()
        return slots

    monkeypatch.setattr(data.booking._availability, "list_slots", list_slots)

    async def book(client_id):
        async with async_session_factory() as session:
            try:
                hold = await data.hold(session, client_id)
                await data.booking.confirm_hold(
                    session,
                    hold_id=hold.id,
                    client_id=client_id,
                    now=data.now,
                )
                await session.commit()
                return "confirmed"
            except SlotUnavailableError:
                await session.rollback()
                return "unavailable"

    outcomes = await asyncio.wait_for(asyncio.gather(*(book(c) for c in data.clients)), 5)
    assert sorted(outcomes) == ["confirmed", "unavailable"]
    await assert_one_active(data)


async def test_replayed_reschedule_preserves_active_calendar_entry(booking_data):
    from datetime import timedelta

    data = booking_data
    async with async_session_factory() as session:
        hold = await data.hold(session, data.clients[0])
        summary = await data.booking.confirm_hold(
            session,
            hold_id=hold.id,
            client_id=data.clients[0],
            now=data.now,
        )
        new_hold = await data.hold(session, data.clients[0], data.start + timedelta(hours=1))
        await session.commit()
    for repeated in (False, True):
        async with async_session_factory() as session:
            try:
                await data.booking.confirm_reschedule(
                    session,
                    business_id=data.business_id,
                    client_id=data.clients[0],
                    appointment_id=summary.appointment_id,
                    hold_id=new_hold.id,
                    now=data.now,
                )
                assert not repeated
            except HoldExpiredError:
                assert repeated
            await session.commit()
    await assert_one_active(data)

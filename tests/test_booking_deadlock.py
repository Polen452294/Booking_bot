from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from booking_bot.config import Settings
from booking_bot.services.bookings import BookingService, SlotUnavailableError


@pytest.mark.parametrize("sqlstate", ["40P01", "08006"])
async def test_hold_deadlock_is_a_slot_conflict_but_connection_failure_propagates(sqlstate):
    start = datetime(2030, 1, 2, 9, tzinfo=UTC)
    booking = BookingService(Settings(_env_file=None))
    booking._availability.list_slots = AsyncMock(
        return_value=[
            SimpleNamespace(
                service_start=start,
                service_end=start,
                occupied_start=start,
                occupied_end=start,
                location_id=None,
            )
        ]
    )
    error = DBAPIError("INSERT", {}, SimpleNamespace(sqlstate=sqlstate))
    session = MagicMock()
    session.begin_nested.return_value = AsyncMock()
    session.flush = AsyncMock(side_effect=error)
    expected = SlotUnavailableError if sqlstate == "40P01" else DBAPIError
    with pytest.raises(expected) as caught:
        await booking.create_hold(
            session,
            business_id=uuid4(),
            master_id=uuid4(),
            service_id=uuid4(),
            client_id=uuid4(),
            service_start=start,
            local_date=date(2030, 1, 2),
            now=start,
        )
    if sqlstate == "40P01":
        assert caught.value.__cause__ is error
    else:
        assert caught.value is error

"""Phase 7.5C: real transaction races, bounded history and privacy boundaries."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError

import test_conversations_integration as domain
from booking_bot.db.models import (
    Appointment,
    AuditLog,
    BookingRequest,
    Conversation,
    ConversationMessage,
    ConversationReadState,
    NotificationJob,
    PriceProposal,
    TelegramUser,
)
from booking_bot.db.session import async_session_factory, engine
from booking_bot.domain.conversations import ProposalNotPendingError
from booking_bot.domain.enums import BookingRequestStatus
from booking_bot.services.bookings import SlotUnavailableError
from booking_bot.services.conversation_context import get_request
from booking_bot.services.notification_delivery import NotificationDeliveryService

pytestmark = pytest.mark.integration
data = domain.data
REQUESTS, CHAT, PRICES = domain.REQUESTS, domain.CHAT, domain.PRICES


async def test_concurrent_proposals_have_one_pending_revision(data):
    async with async_session_factory() as session, session.begin():
        request = await domain.create(session, data)
    gate = asyncio.Event()

    async def propose(amount):
        await gate.wait()
        async with async_session_factory() as session, session.begin():
            await PRICES.propose(
                session, **domain.args(data, request, owner=True), amount_minor=amount
            )

    tasks = [asyncio.create_task(propose(amount)) for amount in range(10)]
    gate.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 20)
    async with async_session_factory() as session:
        proposals = await PRICES.list_proposals(session, **domain.args(data, request))
        assert [p.revision for p in proposals] == list(range(10, 0, -1))
        assert [p.status for p in proposals] == ["pending"] + ["superseded"] * 9
        # The DB invariant also survives writes outside the service layer.
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    PriceProposal(
                        booking_request_id=request.id,
                        created_by_user_id=data.owner,
                        revision=11,
                        amount_minor=99,
                        currency="RUB",
                        status="pending",
                    )
                )
                await session.flush()


async def test_accept_waiting_for_replacement_transaction_rejects_old_price(data):
    async with async_session_factory() as session, session.begin():
        request = await domain.create(session, data)
        old = await PRICES.propose(
            session, **domain.args(data, request, owner=True), amount_minor=1_500_000
        )
    started = asyncio.Event()

    async def accept():
        async with async_session_factory() as session, session.begin():
            started.set()
            with pytest.raises(ProposalNotPendingError):
                await PRICES.accept(session, **domain.args(data, request), proposal_id=old.id)

    async with async_session_factory() as replacing:
        async with replacing.begin():
            await get_request(replacing, **domain.args(data, request, owner=True), lock=True)
            task = asyncio.create_task(accept())
            await started.wait()
            new = await PRICES.propose(
                replacing, **domain.args(data, request, owner=True), amount_minor=1_700_000
            )
        await asyncio.wait_for(task, 10)
    async with async_session_factory() as session:
        proposals = await PRICES.list_proposals(session, **domain.args(data, request))
        assert [(p.id, p.status) for p in proposals] == [
            (new.id, "pending"),
            (old.id, "superseded"),
        ]
        assert (
            await REQUESTS.get(session, **domain.args(data, request))
        ).status == "terms_proposed"


@pytest.mark.parametrize("reader", ["owner", "client"])
async def test_concurrent_mark_read_preserves_message_arriving_after_page(data, reader):
    async with async_session_factory() as session, session.begin():
        request = await domain.create(session, data)
        conversation = await CHAT.get(session, **domain.args(data, request))
        last_seen = conversation.last_message_sequence
    actor = getattr(data, reader)
    sender = data.client if reader == "owner" else data.owner
    chat_args = dict(business_id=data.business, conversation_id=conversation.id)

    async def read():
        async with async_session_factory() as session, session.begin():
            await CHAT.mark_read(
                session, **chat_args, actor_user_id=actor, through_sequence=last_seen
            )

    async def send():
        async with async_session_factory() as session, session.begin():
            await CHAT.send_message(session, **chat_args, actor_user_id=sender, text="new")

    await asyncio.wait_for(asyncio.gather(read(), send()), 10)
    async with async_session_factory() as session, session.begin():
        await CHAT.mark_read(session, **chat_args, actor_user_id=actor, through_sequence=0)
        assert await CHAT.unread_count(session, **chat_args, actor_user_id=actor) == 1


async def test_cached_read_state_cannot_move_cursor_backwards(data):
    async with async_session_factory() as session, session.begin():
        request = await domain.create(session, data)
        conversation = await CHAT.get(session, **domain.args(data, request))
        chat_args = dict(
            business_id=data.business, conversation_id=conversation.id, actor_user_id=data.owner
        )
        await CHAT.mark_read(session, **chat_args, through_sequence=1)
        last = conversation.last_message_sequence
    async with async_session_factory() as stale:
        cached = await stale.get(ConversationReadState, (conversation.id, data.owner))
        assert cached.last_read_sequence == 1
        async with async_session_factory() as fresh, fresh.begin():
            await CHAT.mark_read(fresh, **chat_args, through_sequence=last)
        await CHAT.mark_read(stale, **chat_args, through_sequence=1)
        await stale.commit()
        assert cached.last_read_sequence == last
    async with async_session_factory() as session:
        state = await session.get(ConversationReadState, (conversation.id, data.owner))
        assert state.last_read_sequence == last


@pytest.mark.parametrize("size", [10, 100, 1000])
async def test_history_pages_are_bounded_complete_with_identical_timestamps(data, size):
    async with async_session_factory() as session, session.begin():
        request = await domain.create(session, data)
        conversation = await CHAT.get(session, **domain.args(data, request))
        initial = conversation.last_message_sequence
        conversation.last_message_sequence += size
        session.add_all(
            [
                ConversationMessage(
                    conversation_id=conversation.id,
                    sequence=initial + i,
                    sender_user_id=data.client,
                    sender_role="client",
                    message_type="text",
                    text=f"message {i}",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
                for i in range(1, size + 1)
            ]
        )
    cursor, sequences = 0, []
    while True:
        async with async_session_factory() as session:
            page = await CHAT.list_messages(
                session,
                business_id=data.business,
                conversation_id=conversation.id,
                actor_user_id=data.owner,
                limit=20,
                after_sequence=cursor,
            )
            assert len(page) <= 20
            assert (
                sum(isinstance(obj, ConversationMessage) for obj in session.identity_map.values())
                <= 20
            )
            if not page:
                break
            sequences.extend(m.sequence for m in page)
            cursor = page[-1].sequence
    assert sequences == list(range(1, initial + size + 1))


async def test_closed_notification_and_audit_do_not_copy_private_content(data):
    private = "private proposal comment <b>confidential</b>"
    async with async_session_factory() as session, session.begin():
        request = await domain.create(session, data)
        conversation = await CHAT.get(session, **domain.args(data, request))
        await CHAT.send_message(
            session,
            business_id=data.business,
            conversation_id=conversation.id,
            actor_user_id=data.owner,
            text=private,
        )
        await PRICES.propose(
            session, **domain.args(data, request, owner=True), amount_minor=1, comment=private
        )
        await PRICES.propose(session, **domain.args(data, request, owner=True), amount_minor=2)
        await REQUESTS.close(session, **domain.args(data, request, owner=True))
        await REQUESTS.close(session, **domain.args(data, request, owner=True))
        audits = list(
            await session.scalars(select(AuditLog).where(AuditLog.business_id == data.business))
        )
        assert {
            "request_created",
            "message_created",
            "price_proposed",
            "price_superseded",
            "conversation_closed",
        } <= {a.action for a in audits}
        assert private not in json.dumps([a.details for a in audits])
        jobs = list(
            await session.scalars(
                select(NotificationJob).where(
                    NotificationJob.booking_request_id == request.id,
                    NotificationJob.kind == "client_booking_request_closed",
                )
            )
        )
        assert len(jobs) == 1
        payload = await NotificationDeliveryService(domain.SETTINGS)._build_payload(
            session, jobs[0]
        )
        assert "закрыл" in payload.text and private not in payload.text


async def test_worker_skips_superseded_price_jobs_and_restart_does_not_redeliver(data):
    async with async_session_factory() as session, session.begin():
        request = await domain.create(session, data)
        await PRICES.propose(
            session, **domain.args(data, request, owner=True), amount_minor=1_500_000
        )
        latest = await PRICES.propose(
            session, **domain.args(data, request, owner=True), amount_minor=1_700_000
        )
        await session.execute(
            text(
                "UPDATE notification_jobs SET state='cancelled' "
                "WHERE booking_request_id=:id AND kind!='client_price_proposal'"
            ),
            {"id": request.id},
        )
    bot = AsyncMock()
    for _ in range(2):
        await NotificationDeliveryService(domain.SETTINGS).run_once(bot, business_id=data.business)
    assert bot.send_message.await_count == 1
    assert latest.id.hex in str(bot.send_message.call_args)
    async with async_session_factory() as session:
        states = list(
            await session.scalars(
                select(NotificationJob.state).where(
                    NotificationJob.booking_request_id == request.id,
                    NotificationJob.kind == "client_price_proposal",
                )
            )
        )
        assert sorted(states) == ["cancelled", "sent"]


async def test_batch_unread_does_not_expose_another_clients_requests(data):
    async with async_session_factory() as session, session.begin():
        request = await domain.create(session, data)
        assert (
            await CHAT.unread_by_request(
                session,
                business_id=data.business,
                actor_user_id=data.stranger,
                request_ids=[request.id],
            )
            == {}
        )


@pytest.mark.parametrize(
    "status", ["completed", "no_show", "cancelled_by_master", "cancelled_by_client"]
)
async def test_request_appointment_supports_reschedule_notes_calendar_and_terminal_operations(
    data, status
):
    from datetime import timedelta

    from booking_bot.bot.handlers.booking import _build_appointment_ics
    from booking_bot.db.models import Master
    from booking_bot.services.master_schedule import MasterScheduleService

    booking = domain.BookingService(domain.SETTINGS)
    async with async_session_factory() as session, session.begin():
        request, proposal = await domain.agreed(session, data)
        hold = await REQUESTS.create_hold(
            session,
            **domain.args(data, request),
            settings=domain.SETTINGS,
            local_date=domain.DAY,
            service_start=domain.START,
            now=domain.NOW,
        )
        summary = await REQUESTS.book(
            session,
            **domain.args(data, request),
            settings=domain.SETTINGS,
            hold_id=hold.id,
            now=domain.NOW,
        )
        appointment_id = summary.appointment_id
        master = await session.get(Master, data.master)
        master_service = MasterScheduleService()
        await master_service.set_internal_note(
            session,
            business_id=data.business,
            master=master,
            appointment_id=appointment_id,
            actor_user_id=data.owner,
            note="operator note",
        )
        new_hold = await booking.create_hold(
            session,
            business_id=data.business,
            master_id=data.master,
            service_id=data.service,
            client_id=data.client,
            local_date=domain.DAY,
            service_start=domain.START + timedelta(hours=1),
            now=domain.NOW,
        )
        moved = await booking.confirm_reschedule(
            session,
            business_id=data.business,
            client_id=data.client,
            appointment_id=appointment_id,
            hold_id=new_hold.id,
            now=domain.NOW,
        )
        assert b"BEGIN:VEVENT" in _build_appointment_ics(moved)
        assert moved.booking_request_id == request.id
        if status == "cancelled_by_client":
            await booking.cancel_appointment(
                session,
                business_id=data.business,
                client_id=data.client,
                appointment_id=appointment_id,
                now=domain.NOW,
            )
        else:
            await master_service.change_appointment_status(
                session,
                business_id=data.business,
                master=master,
                appointment_id=appointment_id,
                actor_user_id=data.owner,
                new_status=status,
                now=domain.START + timedelta(hours=3),
            )
        appointment = await session.get(Appointment, appointment_id)
        assert appointment.status == status
        assert appointment.price_minor == proposal.amount_minor == 1_500_000
        assert appointment.internal_note == "operator note"
        assert request.status == "booked"
        conversation = await CHAT.get(session, **domain.args(data, request))
        await CHAT.send_message(
            session,
            business_id=data.business,
            conversation_id=conversation.id,
            actor_user_id=data.client,
            text="history continues",
        )


async def test_two_agreed_clients_compete_for_one_calendar_slot(data, monkeypatch):
    from booking_bot.services.availability import AvailabilityService

    original = AvailabilityService.list_slots
    ready = asyncio.Event()
    calls = 0

    async def concurrent_slots(self, *args, **kwargs):
        nonlocal calls
        slots = await original(self, *args, **kwargs)
        calls += 1
        if calls == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 10)
        return slots

    monkeypatch.setattr(AvailabilityService, "list_slots", concurrent_slots)
    async with async_session_factory() as session, session.begin():
        requests = []
        for client in (data.client, data.stranger):
            request = await REQUESTS.create(
                session,
                business_id=data.business,
                master_id=data.master,
                service_id=data.service,
                actor_user_id=client,
                description="request",
            )
            proposal = await PRICES.propose(
                session,
                business_id=data.business,
                request_id=request.id,
                actor_user_id=data.owner,
                amount_minor=1_700_000,
            )
            await PRICES.accept(
                session,
                business_id=data.business,
                request_id=request.id,
                actor_user_id=client,
                proposal_id=proposal.id,
            )
            requests.append((request.id, client))

    async def book(request_id, client):
        async with async_session_factory() as session, session.begin():
            try:
                hold = await REQUESTS.create_hold(
                    session,
                    settings=domain.SETTINGS,
                    business_id=data.business,
                    request_id=request_id,
                    actor_user_id=client,
                    local_date=domain.DAY,
                    service_start=domain.START,
                    now=domain.NOW,
                )
                await REQUESTS.book(
                    session,
                    settings=domain.SETTINGS,
                    business_id=data.business,
                    request_id=request_id,
                    actor_user_id=client,
                    hold_id=hold.id,
                    now=domain.NOW,
                )
                return True
            except SlotUnavailableError:
                return False

    results = await asyncio.wait_for(asyncio.gather(*(book(*r) for r in requests)), 15)
    assert sorted(results) == [False, True]
    async with async_session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Appointment)
                .where(Appointment.business_id == data.business)
            )
            == 1
        )


async def test_realistic_inbox_and_history_performance_smoke(data):
    async with async_session_factory() as session, session.begin():
        users = [TelegramUser(first_name=f"load {i}", phone="+79990000000") for i in range(100)]
        session.add_all(users)
        await session.flush()
        requests = [
            BookingRequest(
                business_id=data.business,
                master_id=data.master,
                service_id=data.service,
                client_user_id=users[i % 100].id,
                status="waiting_master" if i < 100 else "closed",
                service_name_snapshot="load",
                client_name_snapshot="load",
                client_phone_snapshot="+79990000000",
                description="load",
            )
            for i in range(200)
        ]
        session.add_all(requests)
        await session.flush()
        conversations = [
            Conversation(booking_request_id=r.id, last_message_sequence=20) for r in requests
        ]
        session.add_all(conversations)
        await session.flush()
        session.add_all(
            [
                ConversationMessage(
                    conversation_id=c.id,
                    sequence=i,
                    sender_user_id=r.client_user_id,
                    sender_role="client",
                    message_type="text",
                    text="load",
                )
                for r, c in zip(requests, conversations, strict=True)
                for i in range(1, 21)
            ]
        )
        user_ids = [u.id for u in users]
    queries = []

    def count_queries(conn, cursor, statement, parameters, context, many):
        queries.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", count_queries)
    try:
        async with async_session_factory() as session:
            start = perf_counter()
            seen = []
            for offset in range(0, 100, 20):
                page = await REQUESTS.list_requests(
                    session,
                    business_id=data.business,
                    actor_user_id=data.owner,
                    statuses=[BookingRequestStatus.WAITING_MASTER],
                    limit=20,
                    offset=offset,
                )
                before = len(queries)
                counts = await CHAT.unread_by_request(
                    session,
                    business_id=data.business,
                    actor_user_id=data.owner,
                    request_ids=[r.id for r in page],
                )
                assert len(queries) - before == 1
                assert set(counts.values()) == {20}
                seen.extend(r.id for r in page)
            assert len(seen) == len(set(seen)) == 100
            inbox_ms = (perf_counter() - start) * 1000
            start = perf_counter()
            history = await CHAT.list_messages(
                session,
                business_id=data.business,
                conversation_id=conversations[0].id,
                actor_user_id=data.owner,
                limit=20,
            )
            assert len(history) == 20
            history_ms = (perf_counter() - start) * 1000
            start = perf_counter()
            assert (
                await CHAT.total_unread(
                    session, business_id=data.business, actor_user_id=data.owner
                )
                == 4000
            )
            unread_ms = (perf_counter() - start) * 1000
            unread_sql, unread_params = queries[-1]
            connection = await session.connection()
            plans = {
                "total_unread": (
                    await connection.exec_driver_sql(
                        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + unread_sql,
                        unread_params,
                    )
                ).scalar_one()
            }
            for name, sql, params in [
                (
                    "history",
                    "SELECT * FROM conversation_messages WHERE conversation_id=:id "
                    "ORDER BY sequence DESC LIMIT 20",
                    {"id": conversations[0].id},
                ),
                (
                    "inbox",
                    "SELECT * FROM booking_requests WHERE business_id=:id "
                    "AND status='waiting_master' ORDER BY created_at DESC, id DESC LIMIT 20",
                    {"id": data.business},
                ),
            ]:
                plans[name] = await session.scalar(
                    text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql), params
                )
            start = perf_counter()
            proposal = await PRICES.propose(
                session,
                business_id=data.business,
                request_id=requests[0].id,
                actor_user_id=data.owner,
                amount_minor=1_700_000,
            )
            proposal_ms = (perf_counter() - start) * 1000
            await PRICES.accept(
                session,
                business_id=data.business,
                request_id=requests[0].id,
                actor_user_id=user_ids[0],
                proposal_id=proposal.id,
            )
            start = perf_counter()
            hold = await REQUESTS.create_hold(
                session,
                business_id=data.business,
                request_id=requests[0].id,
                actor_user_id=user_ids[0],
                settings=domain.SETTINGS,
                local_date=domain.DAY,
                service_start=domain.START,
                now=domain.NOW,
            )
            summary = await REQUESTS.book(
                session,
                business_id=data.business,
                request_id=requests[0].id,
                actor_user_id=user_ids[0],
                settings=domain.SETTINGS,
                hold_id=hold.id,
                now=domain.NOW,
            )
            await session.commit()
            booking_ms = (perf_counter() - start) * 1000
            appointment = await session.get(Appointment, summary.appointment_id)
            assert appointment.price_minor == 1_700_000
            # A fresh checkout/CI runner has no ignored tmp directory yet.
            await asyncio.to_thread(Path("tmp").mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(
                Path("tmp/phase75c-performance.json").write_text,
                json.dumps(
                    dict(
                        clients=100,
                        requests=200,
                        messages=4000,
                        inbox_100_ms=inbox_ms,
                        history_20_ms=history_ms,
                        unread_ms=unread_ms,
                        proposal_ms=proposal_ms,
                        hold_booking_ms=booking_ms,
                        plans=plans,
                    ),
                    indent=2,
                ),
                encoding="utf-8",
            )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_queries)
        # Business fixture deletes these requests first; remove extra users afterwards.
        async with async_session_factory() as session, session.begin():
            from sqlalchemy import delete

            await session.execute(
                delete(Appointment).where(Appointment.business_id == data.business)
            )
            await session.execute(
                delete(BookingRequest).where(BookingRequest.id.in_([r.id for r in requests]))
            )
            await session.execute(delete(TelegramUser).where(TelegramUser.id.in_(user_ids)))

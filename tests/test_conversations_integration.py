import asyncio
from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError

from booking_bot.config import Settings
from booking_bot.db.models import (
    Appointment,
    AuditLog,
    BookingRequest,
    Business,
    BusinessMember,
    ConversationMessage,
    Master,
    MasterService,
    NotificationJob,
    PriceProposal,
    Service,
    SlotHold,
    TelegramUser,
    WorkingRule,
)
from booking_bot.db.session import async_session_factory
from booking_bot.domain.conversations import (
    ConversationAccessError,
    ConversationClosedError,
    InvalidRequestTransitionError,
    ProposalNotPendingError,
)
from booking_bot.services.booking_requests import BookingRequestService
from booking_bot.services.bookings import AppointmentChangeNotAllowedError, BookingService
from booking_bot.services.conversations import ConversationService
from booking_bot.services.notification_delivery import NotificationDeliveryService
from booking_bot.services.price_proposals import PriceProposalService

pytestmark = pytest.mark.integration
REQUESTS = BookingRequestService()
CHAT = ConversationService()
PRICES = PriceProposalService()
SETTINGS = Settings(_env_file=None)
NOW = datetime(2026, 10, 1, 6, tzinfo=UTC)
DAY = date(2026, 10, 5)
START = datetime(2026, 10, 5, 12, tzinfo=UTC)


@pytest_asyncio.fixture(loop_scope="session")
async def data():
    async with async_session_factory() as session, session.begin():
        business = Business(slug=f"conversation-{uuid4().hex}", name="Test")
        users = [
            TelegramUser(
                first_name=f"User {i}",
                phone="+79990000000",
                telegram_user_id=-(uuid4().int % 2_000_000_000),
            )
            for i in range(3)
        ]
        session.add_all([business, *users])
        await session.flush()
        master = Master(business_id=business.id, user_id=users[0].id, display_name="Master")
        service = Service(
            business_id=business.id,
            name="Individual service",
            duration_minutes=60,
            pricing_mode="negotiable",
            price_minor=50000,
        )
        session.add_all([master, service])
        await session.flush()
        session.add_all(
            [
                BusinessMember(business_id=business.id, user_id=users[0].id, role="owner"),
                MasterService(business_id=business.id, master_id=master.id, service_id=service.id),
                WorkingRule(
                    business_id=business.id,
                    master_id=master.id,
                    weekday=DAY.weekday(),
                    start_time=time(15),
                    end_time=time(18),
                ),
            ]
        )
    ctx = SimpleNamespace(
        business=business.id,
        master=master.id,
        service=service.id,
        owner=users[0].id,
        client=users[1].id,
        stranger=users[2].id,
    )
    yield ctx
    async with async_session_factory() as session, session.begin():
        await session.execute(delete(Appointment).where(Appointment.business_id == ctx.business))
        await session.execute(
            delete(BookingRequest).where(BookingRequest.business_id == ctx.business)
        )
        await session.execute(delete(Business).where(Business.id == ctx.business))
        await session.execute(
            delete(TelegramUser).where(TelegramUser.id.in_([u.id for u in users]))
        )


async def create(session, data, **kwargs):
    return await REQUESTS.create(
        session,
        business_id=data.business,
        master_id=data.master,
        service_id=data.service,
        actor_user_id=data.client,
        description="Хочу размер 10 см",
        **kwargs,
    )


def args(data, request, *, owner=False):
    return dict(
        business_id=data.business,
        request_id=request.id,
        actor_user_id=data.owner if owner else data.client,
    )


async def agreed(session, data):
    request = await create(session, data)
    proposal = await PRICES.propose(
        session, **args(data, request, owner=True), amount_minor=1500000
    )
    await PRICES.accept(session, **args(data, request), proposal_id=proposal.id)
    return request, proposal


async def test_create_draft_submit_cancel_and_read_closed_history(data):
    async with async_session_factory() as session, session.begin():
        request = await create(session, data, submit=False)
        assert request.status == "draft"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(NotificationJob)
                .where(NotificationJob.booking_request_id == request.id)
            )
            == 0
        )
        await REQUESTS.submit(session, **args(data, request))
        assert request.status == "waiting_master"
        conversation = await CHAT.get(session, **args(data, request))
        await REQUESTS.cancel(session, **args(data, request))
        assert request.status == "cancelled"
        messages = await CHAT.list_messages(
            session,
            business_id=data.business,
            conversation_id=conversation.id,
            actor_user_id=data.client,
        )
        assert messages[-1].event_type == "request_cancelled"
        with pytest.raises(InvalidRequestTransitionError):
            await REQUESTS.submit(session, **args(data, request))
        with pytest.raises(InvalidRequestTransitionError):
            await PRICES.propose(session, **args(data, request, owner=True), amount_minor=1)


async def test_messages_media_pagination_unread_and_close(data):
    async with async_session_factory() as session, session.begin():
        request = await create(session, data)
        conversation = await CHAT.get(session, **args(data, request))
        client_args = dict(
            business_id=data.business, conversation_id=conversation.id, actor_user_id=data.client
        )
        owner_args = {**client_args, "actor_user_id": data.owner}
        assert await CHAT.unread_count(session, **owner_args) == 3
        await CHAT.mark_read(session, **owner_args, through_sequence=3)
        await CHAT.send_message(session, **client_args, text="Лучше 15 см")
        photo = await CHAT.send_message(
            session,
            **client_args,
            message_type="photo",
            telegram_file_id="photo-file",
            telegram_file_unique_id="photo-unique",
        )
        document = await CHAT.send_message(
            session,
            **owner_args,
            message_type="document",
            text="Референс",
            telegram_file_id="doc-file",
            telegram_file_unique_id="doc-unique",
        )
        assert photo.telegram_file_id == "photo-file"
        assert document.telegram_file_unique_id == "doc-unique"
        assert request.status == "waiting_client"
        assert await CHAT.unread_count(session, **owner_args) == 2
        assert await CHAT.unread_count(session, **client_args) == 1
        await CHAT.mark_read(session, **owner_args, through_sequence=6)
        await CHAT.mark_read(session, **owner_args, through_sequence=2)
        assert await CHAT.unread_count(session, **owner_args) == 0
        with pytest.raises(ValueError):
            await CHAT.mark_read(session, **owner_args, through_sequence=100)
        page = await CHAT.list_messages(session, **client_args, limit=2)
        next_page = await CHAT.list_messages(
            session, **client_args, limit=2, after_sequence=page[-1].sequence
        )
        assert [m.sequence for m in page + next_page] == [1, 2, 3, 4]
        with pytest.raises(ConversationAccessError):
            await CHAT.close(session, **client_args)
        await CHAT.close(session, **owner_args)
        assert request.status == "closed"
        with pytest.raises(ConversationClosedError):
            await CHAT.send_message(session, **client_args, text="after close")
        assert (await CHAT.list_messages(session, **client_args))[
            -1
        ].event_type == "conversation_closed"


async def test_isolation_on_every_boundary_and_revoked_master(data):
    async with async_session_factory() as session, session.begin():
        request = await create(session, data)
        conversation = await CHAT.get(session, **args(data, request))
        stranger = {**args(data, request), "actor_user_id": data.stranger}
        chat_args = dict(
            business_id=data.business, conversation_id=conversation.id, actor_user_id=data.stranger
        )
        for operation in [
            REQUESTS.get(session, **stranger),
            REQUESTS.cancel(session, **stranger),
            CHAT.get(session, **stranger),
            CHAT.list_messages(session, **chat_args),
            CHAT.unread_count(session, **chat_args),
            CHAT.mark_read(session, **chat_args, through_sequence=1),
            CHAT.send_message(session, **chat_args, text="attack"),
            CHAT.close(session, **chat_args),
            PRICES.propose(session, **stranger, amount_minor=1),
            PRICES.list_proposals(session, **stranger),
            PRICES.accept(session, **stranger, proposal_id=uuid4()),
            PRICES.reject(session, **stranger, proposal_id=uuid4()),
        ]:
            with pytest.raises(ConversationAccessError):
                await operation
        assert (
            await REQUESTS.list_requests(
                session, business_id=data.business, actor_user_id=data.stranger
            )
            == []
        )
        with pytest.raises(ConversationAccessError):
            await REQUESTS.get(session, **{**args(data, request), "business_id": uuid4()})
        await session.execute(
            update(BusinessMember)
            .where(BusinessMember.user_id == data.owner)
            .values(is_active=False)
        )
        with pytest.raises(ConversationAccessError):
            await REQUESTS.get(session, **args(data, request, owner=True))


async def test_proposal_supersede_reject_accept_and_accepted_history(data):
    async with async_session_factory() as session, session.begin():
        request = await create(session, data)
        a = await PRICES.propose(session, **args(data, request, owner=True), amount_minor=1500000)
        b = await PRICES.propose(session, **args(data, request, owner=True), amount_minor=1700000)
        assert a.status == "superseded" and a.superseded_at is not None
        with pytest.raises(ProposalNotPendingError):
            await PRICES.accept(session, **args(data, request), proposal_id=a.id)
        await PRICES.reject(session, **args(data, request), proposal_id=b.id)
        assert b.status == "rejected" and request.status == "waiting_master"
        c = await PRICES.propose(session, **args(data, request, owner=True), amount_minor=1800000)
        await PRICES.accept(session, **args(data, request), proposal_id=c.id)
        d = await PRICES.propose(session, **args(data, request, owner=True), amount_minor=1900000)
        assert c.status == "accepted" and c.amount_minor == 1800000
        assert request.status == "terms_proposed"
        with pytest.raises(InvalidRequestTransitionError):
            await REQUESTS.create_hold(
                session,
                **args(data, request),
                settings=SETTINGS,
                local_date=DAY,
                service_start=START,
                now=NOW,
            )
        await PRICES.accept(session, **args(data, request), proposal_id=d.id)
        assert [
            p.amount_minor for p in await PRICES.list_proposals(session, **args(data, request))
        ] == [
            1900000,
            1800000,
            1700000,
            1500000,
        ]
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.entity_id == request.id, AuditLog.action == "price_accepted")
            )
            == 2
        )


async def test_master_membership_in_another_business_does_not_grant_access(data):
    async with async_session_factory() as session, session.begin():
        request = await create(session, data)
        other = Business(slug=f"other-conversation-{uuid4().hex}", name="Other business")
        session.add(other)
        await session.flush()
        other_master = Master(business_id=other.id, user_id=data.owner, display_name="Other master")
        other_service = Service(
            business_id=other.id, name="Other", duration_minutes=60, pricing_mode="negotiable"
        )
        session.add_all([other_master, other_service])
        await session.flush()
        session.add_all(
            [
                BusinessMember(business_id=other.id, user_id=data.owner, role="owner"),
                MasterService(
                    business_id=other.id, master_id=other_master.id, service_id=other_service.id
                ),
            ]
        )
        await session.flush()
        other_request = await REQUESTS.create(
            session,
            business_id=other.id,
            master_id=other_master.id,
            service_id=other_service.id,
            actor_user_id=data.stranger,
            description="Other requirements",
        )
        await session.execute(
            update(BusinessMember)
            .where(
                BusinessMember.business_id == data.business,
                BusinessMember.user_id == data.owner,
            )
            .values(is_active=False)
        )
        with pytest.raises(ConversationAccessError):
            await REQUESTS.get(session, **args(data, request, owner=True))
        assert (
            await REQUESTS.list_requests(
                session, business_id=data.business, actor_user_id=data.owner
            )
            == []
        )
        assert (
            await REQUESTS.get(
                session, business_id=other.id, request_id=other_request.id, actor_user_id=data.owner
            )
        ).id == other_request.id
        await session.execute(delete(BookingRequest).where(BookingRequest.id == other_request.id))
        await session.execute(delete(Business).where(Business.id == other.id))


async def test_booking_uses_existing_engine_and_preserves_snapshot_and_conversation(data):
    async with async_session_factory() as session, session.begin():
        request, proposal = await agreed(session, data)
        slots = await REQUESTS.list_slots(
            session, **args(data, request), settings=SETTINGS, local_date=DAY, now=NOW
        )
        assert any(slot.service_start == START for slot in slots)
        hold = await REQUESTS.create_hold(
            session,
            **args(data, request),
            settings=SETTINGS,
            local_date=DAY,
            service_start=START,
            now=NOW,
        )
        await session.execute(
            update(Service).where(Service.id == data.service).values(price_minor=999)
        )
        summary = await REQUESTS.book(
            session, **args(data, request), settings=SETTINGS, hold_id=hold.id, now=NOW
        )
        appointment = await session.get(Appointment, summary.appointment_id)
        assert appointment.price_minor == proposal.amount_minor == 1500000
        assert appointment.booking_request_id == request.id and request.status == "booked"
        conversation = await CHAT.get(session, **args(data, request))
        assert conversation.status == "open"
        await CHAT.send_message(
            session,
            business_id=data.business,
            conversation_id=conversation.id,
            actor_user_id=data.client,
            text="До встречи",
        )
        with pytest.raises(InvalidRequestTransitionError):
            await REQUESTS.cancel(session, **args(data, request))
        await CHAT.close(
            session,
            business_id=data.business,
            conversation_id=conversation.id,
            actor_user_id=data.owner,
        )
        assert request.status == "booked"


async def test_direct_negotiable_booking_cannot_bypass_agreement_and_from_supports_both(data):
    async with async_session_factory() as session, session.begin():
        booking = BookingService(SETTINGS)
        hold = await booking.create_hold(
            session,
            business_id=data.business,
            master_id=data.master,
            service_id=data.service,
            client_id=data.client,
            local_date=DAY,
            service_start=START,
            now=NOW,
        )
        with pytest.raises(AppointmentChangeNotAllowedError):
            await booking.confirm_hold(session, hold_id=hold.id, client_id=data.client, now=NOW)
        await session.execute(
            update(Service).where(Service.id == data.service).values(pricing_mode="from")
        )
        request = await create(session, data)
        assert request.status == "waiting_master"
        summary = await booking.confirm_hold(
            session, hold_id=hold.id, client_id=data.client, now=NOW
        )
        appointment = await session.get(Appointment, summary.appointment_id)
        assert appointment.booking_request_id is None and appointment.price_minor == 50000


async def test_notification_jobs_all_kinds_deduplicate_and_worker_understands_them(data):
    async with async_session_factory() as session, session.begin():
        request = await create(session, data)
        conversation = await CHAT.get(session, **args(data, request))
        for actor in (data.client, data.owner):
            await CHAT.send_message(
                session,
                business_id=data.business,
                conversation_id=conversation.id,
                actor_user_id=actor,
                text="<script>hello</script>",
            )
        p = await PRICES.propose(session, **args(data, request, owner=True), amount_minor=1)
        await PRICES.reject(session, **args(data, request), proposal_id=p.id)
        p = await PRICES.propose(session, **args(data, request, owner=True), amount_minor=2)
        await PRICES.accept(session, **args(data, request), proposal_id=p.id)
        with pytest.raises(ProposalNotPendingError):
            await PRICES.accept(session, **args(data, request), proposal_id=p.id)
        jobs = list(
            (
                await session.scalars(
                    select(NotificationJob).where(NotificationJob.booking_request_id == request.id)
                )
            ).all()
        )
        assert {job.kind for job in jobs} == {
            "master_new_booking_request",
            "master_new_conversation_message",
            "client_new_conversation_message",
            "client_price_proposal",
            "master_price_proposal_accepted",
            "master_price_proposal_rejected",
        }
        assert len(jobs) == len({job.event_key for job in jobs}) == 7
        worker = NotificationDeliveryService(SETTINGS)
        for job in jobs:
            payload = await worker._build_payload(session, job)
            assert payload.reply_markup is not None
            assert any(
                request.id.hex in button.callback_data
                for row in payload.reply_markup.inline_keyboard
                for button in row
            )
            assert payload.chat_id is not None


async def test_critical_operation_rolls_back_even_if_caller_catches_error(data, monkeypatch):
    async with async_session_factory() as session, session.begin():
        request = await create(session, data)

        async def fail(*args, **kwargs):
            raise RuntimeError("Outbox unavailable")

        monkeypatch.setattr("booking_bot.services.price_proposals.enqueue_notification", fail)
        client_args = args(data, request)
        owner_args = args(data, request, owner=True)
        request_id = request.id
        with pytest.raises(RuntimeError):
            await PRICES.propose(session, **owner_args, amount_minor=123)
        assert (
            await session.scalar(
                select(func.count())
                .select_from(PriceProposal)
                .where(PriceProposal.booking_request_id == request_id)
            )
            == 0
        )
        assert (await REQUESTS.get(session, **client_args)).status == "waiting_master"


async def test_database_rejects_mutating_terms_and_message_history(data):
    async with async_session_factory() as session, session.begin():
        request, proposal = await agreed(session, data)
        conversation = await CHAT.get(session, **args(data, request))
        for statement in [
            update(PriceProposal).where(PriceProposal.id == proposal.id).values(amount_minor=3),
            update(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation.id)
            .values(text="rewritten history"),
        ]:
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(statement)


async def test_concurrent_old_and_current_acceptance(data):
    async with async_session_factory() as session, session.begin():
        request = await create(session, data)
        a = await PRICES.propose(session, **args(data, request, owner=True), amount_minor=1500000)
        b = await PRICES.propose(session, **args(data, request, owner=True), amount_minor=1700000)
    gate = asyncio.Event()

    async def accept(proposal_id):
        await gate.wait()
        async with async_session_factory() as session, session.begin():
            try:
                await PRICES.accept(session, **args(data, request), proposal_id=proposal_id)
                return "accepted"
            except ProposalNotPendingError:
                return "stale"

    tasks = [asyncio.create_task(accept(p.id)) for p in (a, b)]
    gate.set()
    assert await asyncio.wait_for(asyncio.gather(*tasks), 10) == ["stale", "accepted"]


async def test_concurrent_final_booking_creates_only_one_appointment(data):
    async with async_session_factory() as session, session.begin():
        request, _proposal = await agreed(session, data)
        hold = await REQUESTS.create_hold(
            session,
            **args(data, request),
            settings=SETTINGS,
            local_date=DAY,
            service_start=START,
            now=NOW,
        )
    gate = asyncio.Event()

    async def book():
        await gate.wait()
        async with async_session_factory() as session, session.begin():
            try:
                await REQUESTS.book(
                    session, **args(data, request), settings=SETTINGS, hold_id=hold.id, now=NOW
                )
                return "booked"
            except InvalidRequestTransitionError:
                return "already_booked"

    tasks = [asyncio.create_task(book()) for _ in range(2)]
    gate.set()
    assert sorted(await asyncio.wait_for(asyncio.gather(*tasks), 10)) == [
        "already_booked",
        "booked",
    ]
    async with async_session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Appointment)
                .where(Appointment.booking_request_id == request.id)
            )
            == 1
        )


@pytest.mark.parametrize("operation", ["create", "message", "accept", "book"])
async def test_all_critical_writes_rollback_with_outbox_failure(data, monkeypatch, operation):
    async def fail(*args, **kwargs):
        raise RuntimeError("Injected transaction failure")

    async with async_session_factory() as session, session.begin():
        if operation == "create":
            monkeypatch.setattr("booking_bot.services.booking_requests.enqueue_notification", fail)
            with pytest.raises(RuntimeError):
                await create(session, data)
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(BookingRequest)
                    .where(
                        BookingRequest.business_id == data.business,
                    )
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        AuditLog.business_id == data.business,
                    )
                )
                == 0
            )
            return
        request = await create(session, data)
        request_args = args(data, request)
        request_id = request.id
        if operation == "message":
            conversation = await CHAT.get(session, **request_args)
            chat_args = dict(
                business_id=data.business,
                conversation_id=conversation.id,
                actor_user_id=data.client,
            )
            monkeypatch.setattr("booking_bot.services.conversations.enqueue_notification", fail)
            with pytest.raises(RuntimeError):
                await CHAT.send_message(session, **chat_args, text="Must roll back")
            assert len(await CHAT.list_messages(session, **chat_args)) == 3
            assert (await CHAT.get(session, **request_args)).last_message_sequence == 3
        elif operation == "accept":
            proposal = await PRICES.propose(
                session, **args(data, request, owner=True), amount_minor=12
            )
            proposal_id = proposal.id
            monkeypatch.setattr("booking_bot.services.price_proposals.enqueue_notification", fail)
            with pytest.raises(RuntimeError):
                await PRICES.accept(session, **request_args, proposal_id=proposal_id)
            assert (await PRICES.list_proposals(session, **request_args))[0].status == "pending"
            assert (await REQUESTS.get(session, **request_args)).status == "terms_proposed"
        else:
            proposal = await PRICES.propose(
                session, **args(data, request, owner=True), amount_minor=12
            )
            await PRICES.accept(session, **request_args, proposal_id=proposal.id)
            hold = await REQUESTS.create_hold(
                session,
                **request_args,
                settings=SETTINGS,
                local_date=DAY,
                service_start=START,
                now=NOW,
            )
            hold_id = hold.id
            original = BookingService._schedule_notifications

            async def failing_schedule(self, *args, **kwargs):
                await original(self, *args, **kwargs)
                raise RuntimeError("Injected transaction failure")

            monkeypatch.setattr(BookingService, "_schedule_notifications", failing_schedule)
            with pytest.raises(RuntimeError):
                await REQUESTS.book(
                    session, **request_args, settings=SETTINGS, hold_id=hold_id, now=NOW
                )
            assert (await REQUESTS.get(session, **request_args)).status == "terms_accepted"
            assert (await session.get(SlotHold, hold_id)).status == "active"
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Appointment)
                    .where(
                        Appointment.booking_request_id == request_id,
                    )
                )
                == 0
            )


async def test_concurrent_propose_and_accept_require_latest_terms(data):
    async with async_session_factory() as session, session.begin():
        request = await create(session, data)
        proposal = await PRICES.propose(session, **args(data, request, owner=True), amount_minor=10)
    gate = asyncio.Event()

    async def replace():
        await gate.wait()
        async with async_session_factory() as session, session.begin():
            await PRICES.propose(session, **args(data, request, owner=True), amount_minor=20)

    async def accept():
        await gate.wait()
        async with async_session_factory() as session, session.begin():
            try:
                await PRICES.accept(session, **args(data, request), proposal_id=proposal.id)
            except ProposalNotPendingError:
                pass

    tasks = [asyncio.create_task(replace()), asyncio.create_task(accept())]
    gate.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 10)
    async with async_session_factory() as session:
        assert (await REQUESTS.get(session, **args(data, request))).status == "terms_proposed"
        proposals = await PRICES.list_proposals(session, **args(data, request))
        assert proposals[0].status == "pending" and proposals[0].amount_minor == 20
        assert proposals[1].status in {"accepted", "superseded"}

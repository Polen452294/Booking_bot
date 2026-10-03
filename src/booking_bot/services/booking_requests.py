from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from booking_bot.config import Settings
from booking_bot.db.models import (
    BookingRequest,
    Business,
    Conversation,
    Master,
    MasterService,
    PriceProposal,
    Service,
    SlotHold,
    TelegramUser,
)
from booking_bot.domain.conversations import (
    MAX_DESCRIPTION_LENGTH,
    ConversationAccessError,
    InvalidRequestTransitionError,
    validate_page,
    validate_text,
)
from booking_bot.domain.enums import (
    BookingRequestStatus,
    ConversationMessageType,
    ConversationSenderRole,
    ConversationStatus,
    PriceProposalStatus,
    PricingMode,
)
from booking_bot.services.conversation_context import (
    access_predicate,
    append_message,
    enqueue_notification,
    get_conversation,
    get_request,
    latest_proposal,
    record_event,
    require_client,
    require_master,
    transition,
)
from booking_bot.services.users import normalize_phone

if TYPE_CHECKING:
    from booking_bot.services.availability import BookableSlot
    from booking_bot.services.bookings import AppointmentSummary


async def accepted_booking_context(
    session: AsyncSession,
    *,
    business_id: UUID,
    request_id: UUID,
    client_id: UUID,
) -> tuple[BookingRequest, PriceProposal]:
    request = await get_request(
        session,
        business_id=business_id,
        request_id=request_id,
        actor_user_id=client_id,
        lock=True,
    )
    require_client(request, client_id)
    proposal = await latest_proposal(session, request.id)
    if (
        request.status != BookingRequestStatus.TERMS_ACCEPTED.value
        or proposal is None
        or proposal.status != PriceProposalStatus.ACCEPTED.value
    ):
        raise InvalidRequestTransitionError("Current terms must be accepted before booking")
    return request, proposal


class BookingRequestService:
    async def for_appointment(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        appointment_id: UUID,
        actor_user_id: UUID,
    ) -> BookingRequest:
        from booking_bot.db.models import Appointment

        request_id = await session.scalar(
            select(Appointment.booking_request_id).where(
                Appointment.id == appointment_id,
                Appointment.business_id == business_id,
            )
        )
        if request_id is None:
            raise ConversationAccessError("Request is unavailable")
        return await self.get(
            session, business_id=business_id, request_id=request_id, actor_user_id=actor_user_id
        )

    async def create(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        master_id: UUID,
        service_id: UUID,
        actor_user_id: UUID,
        description: str,
        client_name: str | None = None,
        client_phone: str | None = None,
        submit: bool = True,
    ) -> BookingRequest:
        description = validate_text(
            description,
            maximum=MAX_DESCRIPTION_LENGTH,
            field="description",
        )
        async with session.begin_nested():
            service = await session.scalar(
                select(Service)
                .join(
                    MasterService,
                    MasterService.service_id == Service.id,
                )
                .join(Master, Master.id == MasterService.master_id)
                .join(
                    Business,
                    Business.id == Service.business_id,
                )
                .where(
                    Service.id == service_id,
                    Service.business_id == business_id,
                    Service.is_active.is_(True),
                    MasterService.master_id == master_id,
                    MasterService.business_id == business_id,
                    MasterService.is_active.is_(True),
                    Master.business_id == business_id,
                    Master.is_active.is_(True),
                    Master.user_id.is_not(None),
                    Master.user_id != actor_user_id,
                    Business.is_active.is_(True),
                )
            )
            client = await session.get(TelegramUser, actor_user_id)
            if service is None or client is None:
                raise ConversationAccessError("Service is unavailable")
            if service.pricing_mode not in {PricingMode.NEGOTIABLE.value, PricingMode.FROM.value}:
                raise ValueError("Fixed-price services use the existing booking flow")
            name = validate_text(
                client_name
                if client_name is not None
                else " ".join(part for part in (client.first_name, client.last_name) if part),
                maximum=160,
                field="client_name",
            )
            raw_phone = client_phone if client_phone is not None else client.phone or ""
            if not isinstance(raw_phone, str) or len(raw_phone) > 64:
                raise ValueError("Client phone is too long")
            phone = normalize_phone(raw_phone)
            if phone is None:
                raise ValueError("Client phone must contain 10-15 digits")
            request = BookingRequest(
                business_id=business_id,
                master_id=master_id,
                service_id=service_id,
                client_user_id=actor_user_id,
                description=description,
                service_name_snapshot=service.name,
                client_name_snapshot=name,
                client_phone_snapshot=phone,
                status=BookingRequestStatus.DRAFT.value,
            )
            session.add(request)
            await session.flush()
            conversation = Conversation(booking_request_id=request.id)
            session.add(conversation)
            await session.flush()
            await append_message(
                session,
                conversation=conversation,
                actor_user_id=actor_user_id,
                sender_role=ConversationSenderRole.CLIENT,
                message_type=ConversationMessageType.TEXT,
                text=description,
            )
            await record_event(
                session,
                request=request,
                actor_user_id=actor_user_id,
                action="request_created",
            )
            if submit:
                await self.submit(
                    session,
                    business_id=business_id,
                    request_id=request.id,
                    actor_user_id=actor_user_id,
                )
            await session.flush()
            return request

    async def get(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        request_id: UUID,
        actor_user_id: UUID,
    ) -> BookingRequest:
        return await get_request(
            session,
            business_id=business_id,
            request_id=request_id,
            actor_user_id=actor_user_id,
        )

    async def list_requests(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        actor_user_id: UUID,
        statuses: list[BookingRequestStatus] | None = None,
        limit: int = 50,
        offset: int = 0,
        unopened_by_actor: bool = False,
    ) -> list[BookingRequest]:
        validate_page(limit, offset)
        statement = select(BookingRequest).where(
            BookingRequest.business_id == business_id,
            access_predicate(actor_user_id),
        )
        if statuses is not None:
            statement = statement.where(
                BookingRequest.status.in_(
                    [BookingRequestStatus(status).value for status in statuses]
                )
            )
        if unopened_by_actor:
            from sqlalchemy import exists

            from booking_bot.db.models import ConversationReadState

            statement = statement.where(
                ~exists().where(
                    Conversation.booking_request_id == BookingRequest.id,
                    ConversationReadState.conversation_id == Conversation.id,
                    ConversationReadState.user_id == actor_user_id,
                    ConversationReadState.last_read_sequence > 0,
                )
            )
        return list(
            (
                await session.scalars(
                    statement.order_by(
                        BookingRequest.created_at.desc(),
                        BookingRequest.id.desc(),
                    )
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        )

    async def submit(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        request_id: UUID,
        actor_user_id: UUID,
    ) -> BookingRequest:
        async with session.begin_nested():
            request = await get_request(
                session,
                business_id=business_id,
                request_id=request_id,
                actor_user_id=actor_user_id,
                lock=True,
            )
            require_client(request, actor_user_id)
            if request.status != BookingRequestStatus.DRAFT.value:
                raise InvalidRequestTransitionError("Only drafts can be submitted")
            transition(request, BookingRequestStatus.WAITING_MASTER)
            message = await record_event(
                session,
                request=request,
                actor_user_id=actor_user_id,
                action="request_submitted",
            )
            await enqueue_notification(
                session,
                request=request,
                kind="master_new_booking_request",
                event_id=message.id,
            )
            await session.flush()
            return request

    async def cancel(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        request_id: UUID,
        actor_user_id: UUID,
    ) -> BookingRequest:
        return await self._finish(
            session,
            business_id=business_id,
            request_id=request_id,
            actor_user_id=actor_user_id,
            cancel=True,
        )

    async def close(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        request_id: UUID,
        actor_user_id: UUID,
    ) -> BookingRequest:
        return await self._finish(
            session,
            business_id=business_id,
            request_id=request_id,
            actor_user_id=actor_user_id,
            cancel=False,
        )

    async def _finish(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        request_id: UUID,
        actor_user_id: UUID,
        cancel: bool,
    ) -> BookingRequest:
        async with session.begin_nested():
            request = await get_request(
                session,
                business_id=business_id,
                request_id=request_id,
                actor_user_id=actor_user_id,
                lock=True,
            )
            if not cancel:
                require_master(request, actor_user_id)
            conversation = await get_conversation(session, request.id)
            if not cancel and conversation.status == ConversationStatus.CLOSED.value:
                return request
            if cancel or request.status != BookingRequestStatus.BOOKED.value:
                transition(
                    request,
                    BookingRequestStatus.CANCELLED if cancel else BookingRequestStatus.CLOSED,
                )
                request.closed_at = datetime.now(UTC)
            proposal = await latest_proposal(session, request.id)
            if proposal is not None and proposal.status == PriceProposalStatus.PENDING.value:
                proposal.status = PriceProposalStatus.SUPERSEDED.value
                proposal.superseded_at = datetime.now(UTC)
            conversation.status = ConversationStatus.CLOSED.value
            conversation.closed_at = datetime.now(UTC)
            event = await record_event(
                session,
                request=request,
                actor_user_id=actor_user_id,
                action="request_cancelled" if cancel else "conversation_closed",
            )
            if cancel:
                await enqueue_notification(
                    session,
                    request=request,
                    kind="master_booking_request_cancelled",
                    event_id=event.id,
                )
            else:
                await enqueue_notification(
                    session,
                    request=request,
                    kind="client_booking_request_closed",
                    event_id=event.id,
                )
            await session.flush()
            return request

    async def list_slots(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        business_id: UUID,
        request_id: UUID,
        actor_user_id: UUID,
        local_date: date,
        now: datetime | None = None,
    ) -> list[BookableSlot]:
        from booking_bot.services.availability import AvailabilityService

        request, _proposal = await accepted_booking_context(
            session,
            business_id=business_id,
            request_id=request_id,
            client_id=actor_user_id,
        )
        return await AvailabilityService(settings).list_slots(
            session,
            business_id=business_id,
            master_id=request.master_id,
            service_id=request.service_id,
            local_date=local_date,
            now=now,
        )

    async def create_hold(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        business_id: UUID,
        request_id: UUID,
        actor_user_id: UUID,
        local_date: date,
        service_start: datetime,
        now: datetime | None = None,
    ) -> SlotHold:
        from booking_bot.services.bookings import BookingService

        async with session.begin_nested():
            request, _proposal = await accepted_booking_context(
                session,
                business_id=business_id,
                request_id=request_id,
                client_id=actor_user_id,
            )
            return await BookingService(settings).create_hold(
                session,
                business_id=business_id,
                master_id=request.master_id,
                service_id=request.service_id,
                client_id=actor_user_id,
                local_date=local_date,
                service_start=service_start,
                now=now,
            )

    async def book(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        business_id: UUID,
        request_id: UUID,
        actor_user_id: UUID,
        hold_id: UUID,
        now: datetime | None = None,
    ) -> AppointmentSummary:
        from booking_bot.services.bookings import BookingService

        async with session.begin_nested():
            await accepted_booking_context(
                session,
                business_id=business_id,
                request_id=request_id,
                client_id=actor_user_id,
            )
            return await BookingService(settings).confirm_hold(
                session,
                hold_id=hold_id,
                client_id=actor_user_id,
                now=now,
                booking_request_id=request_id,
            )

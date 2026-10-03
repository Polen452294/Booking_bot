from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from booking_bot.db.models import (
    AuditLog,
    BookingRequest,
    Conversation,
    ConversationMessage,
    ConversationReadState,
)
from booking_bot.domain.conversations import (
    ConversationAccessError,
    ConversationClosedError,
    validate_message,
    validate_page,
)
from booking_bot.domain.enums import (
    BookingRequestStatus,
    ConversationMessageType,
    ConversationSenderRole,
    ConversationStatus,
)
from booking_bot.services.conversation_context import (
    append_message,
    enqueue_notification,
    get_conversation,
    get_request,
    transition,
)


async def conversation_context(
    session: AsyncSession,
    *,
    business_id: UUID,
    conversation_id: UUID,
    actor_user_id: UUID,
    lock: bool = False,
) -> tuple[BookingRequest, Conversation]:
    request_id = await session.scalar(
        select(Conversation.booking_request_id).where(Conversation.id == conversation_id)
    )
    if request_id is None:
        raise ConversationAccessError("Conversation is unavailable")
    request = await get_request(
        session,
        business_id=business_id,
        request_id=request_id,
        actor_user_id=actor_user_id,
        lock=lock,
    )
    return request, await get_conversation(session, request.id)


class ConversationService:
    async def unread_by_request(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        actor_user_id: UUID,
        request_ids: list[UUID],
    ) -> dict[UUID, int]:
        """Bounded, authorized unread counts for an inbox page in one query."""
        from booking_bot.services.conversation_context import access_predicate

        if not request_ids:
            return {}
        validate_page(len(request_ids), 0)
        rows = await session.execute(
            select(BookingRequest.id, func.count(ConversationMessage.id))
            .join(Conversation, Conversation.booking_request_id == BookingRequest.id)
            .outerjoin(
                ConversationReadState,
                (ConversationReadState.conversation_id == Conversation.id)
                & (ConversationReadState.user_id == actor_user_id),
            )
            .outerjoin(
                ConversationMessage,
                (ConversationMessage.conversation_id == Conversation.id)
                & (ConversationMessage.sender_user_id != actor_user_id)
                & (
                    ConversationMessage.sequence
                    > func.coalesce(ConversationReadState.last_read_sequence, 0)
                ),
            )
            .where(
                BookingRequest.business_id == business_id,
                BookingRequest.id.in_(request_ids),
                access_predicate(actor_user_id),
            )
            .group_by(BookingRequest.id)
        )
        return dict(rows.all())

    async def total_unread(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        actor_user_id: UUID,
    ) -> int:
        from booking_bot.services.conversation_context import access_predicate

        cursor = (
            select(ConversationReadState.last_read_sequence)
            .where(
                ConversationReadState.conversation_id == Conversation.id,
                ConversationReadState.user_id == actor_user_id,
            )
            .correlate(Conversation)
            .scalar_subquery()
        )
        return (
            await session.scalar(
                select(func.count())
                .select_from(ConversationMessage)
                .join(Conversation, Conversation.id == ConversationMessage.conversation_id)
                .join(BookingRequest, BookingRequest.id == Conversation.booking_request_id)
                .where(
                    BookingRequest.business_id == business_id,
                    access_predicate(actor_user_id),
                    BookingRequest.status != BookingRequestStatus.DRAFT.value,
                    ConversationMessage.sender_user_id != actor_user_id,
                    ConversationMessage.sequence > func.coalesce(cursor, 0),
                )
            )
            or 0
        )

    async def get(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        request_id: UUID,
        actor_user_id: UUID,
    ) -> Conversation:
        await get_request(
            session,
            business_id=business_id,
            request_id=request_id,
            actor_user_id=actor_user_id,
        )
        return await get_conversation(session, request_id)

    async def send_message(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        conversation_id: UUID,
        actor_user_id: UUID,
        message_type: ConversationMessageType = ConversationMessageType.TEXT,
        text: str | None = None,
        telegram_file_id: str | None = None,
        telegram_file_unique_id: str | None = None,
    ) -> ConversationMessage:
        kind, body = validate_message(
            message_type,
            text,
            telegram_file_id,
            telegram_file_unique_id,
        )
        async with session.begin_nested():
            request, conversation = await conversation_context(
                session,
                business_id=business_id,
                conversation_id=conversation_id,
                actor_user_id=actor_user_id,
                lock=True,
            )
            if conversation.status != ConversationStatus.OPEN.value:
                raise ConversationClosedError("Conversation is closed")
            client = request.client_user_id == actor_user_id
            message = await append_message(
                session,
                conversation=conversation,
                actor_user_id=actor_user_id,
                sender_role=ConversationSenderRole.CLIENT
                if client
                else ConversationSenderRole.MASTER,
                message_type=kind,
                text=body,
                telegram_file_id=telegram_file_id,
                telegram_file_unique_id=telegram_file_unique_id,
            )
            if request.status in {
                BookingRequestStatus.WAITING_MASTER.value,
                BookingRequestStatus.WAITING_CLIENT.value,
            }:
                target = (
                    BookingRequestStatus.WAITING_MASTER
                    if client
                    else BookingRequestStatus.WAITING_CLIENT
                )
                if request.status != target.value:
                    transition(request, target)
            if request.status != BookingRequestStatus.DRAFT.value:
                await enqueue_notification(
                    session,
                    request=request,
                    event_id=message.id,
                    kind="master_new_conversation_message"
                    if client
                    else "client_new_conversation_message",
                )
            session.add(
                AuditLog(
                    business_id=request.business_id,
                    actor_user_id=actor_user_id,
                    action="message_created",
                    entity_type="conversation",
                    entity_id=conversation.id,
                    details={"message_id": str(message.id), "request_id": str(request.id)},
                )
            )
            await session.flush()
            return message

    async def list_messages(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        conversation_id: UUID,
        actor_user_id: UUID,
        limit: int = 50,
        after_sequence: int = 0,
    ) -> list[ConversationMessage]:
        validate_page(limit, after_sequence)
        await conversation_context(
            session,
            business_id=business_id,
            conversation_id=conversation_id,
            actor_user_id=actor_user_id,
        )
        return list(
            (
                await session.scalars(
                    select(ConversationMessage)
                    .where(
                        ConversationMessage.conversation_id == conversation_id,
                        ConversationMessage.sequence > after_sequence,
                    )
                    .order_by(ConversationMessage.sequence)
                    .limit(limit)
                )
            ).all()
        )

    async def unread_count(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        conversation_id: UUID,
        actor_user_id: UUID,
    ) -> int:
        await conversation_context(
            session,
            business_id=business_id,
            conversation_id=conversation_id,
            actor_user_id=actor_user_id,
        )
        cursor = (
            select(ConversationReadState.last_read_sequence)
            .where(
                ConversationReadState.conversation_id == conversation_id,
                ConversationReadState.user_id == actor_user_id,
            )
            .scalar_subquery()
        )
        return (
            await session.scalar(
                select(func.count())
                .select_from(ConversationMessage)
                .where(
                    ConversationMessage.conversation_id == conversation_id,
                    ConversationMessage.sequence > func.coalesce(cursor, 0),
                    ConversationMessage.sender_user_id != actor_user_id,
                )
            )
            or 0
        )

    async def mark_read(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        conversation_id: UUID,
        actor_user_id: UUID,
        through_sequence: int,
    ) -> None:
        validate_page(1, through_sequence)
        async with session.begin_nested():
            _request, conversation = await conversation_context(
                session,
                business_id=business_id,
                conversation_id=conversation_id,
                actor_user_id=actor_user_id,
                lock=True,
            )
            if through_sequence > conversation.last_message_sequence:
                raise ValueError("Cannot mark future messages as read")
            state = await session.get(
                ConversationReadState, (conversation_id, actor_user_id), populate_existing=True
            )
            if state is None:
                session.add(
                    ConversationReadState(
                        conversation_id=conversation_id,
                        user_id=actor_user_id,
                        last_read_sequence=through_sequence,
                    )
                )
            else:
                state.last_read_sequence = max(state.last_read_sequence, through_sequence)
            await session.flush()

    async def close(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        conversation_id: UUID,
        actor_user_id: UUID,
    ) -> None:
        # Share the request lifecycle operation so closing also withdraws pending terms.
        from booking_bot.services.booking_requests import BookingRequestService

        request, _conversation = await conversation_context(
            session,
            business_id=business_id,
            conversation_id=conversation_id,
            actor_user_id=actor_user_id,
        )
        await BookingRequestService().close(
            session,
            business_id=business_id,
            request_id=request.id,
            actor_user_id=actor_user_id,
        )

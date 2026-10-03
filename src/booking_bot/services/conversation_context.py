"""Shared authorization, lock order, append-only history and transactional outbox helpers."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from booking_bot.db.models import (
    AuditLog,
    BookingRequest,
    BusinessMember,
    Conversation,
    ConversationMessage,
    Master,
    NotificationJob,
    PriceProposal,
)
from booking_bot.domain.conversations import ConversationAccessError, check_transition
from booking_bot.domain.enums import (
    BookingRequestStatus,
    ConversationMessageType,
    ConversationSenderRole,
    MemberRole,
)

REQUEST_NOTIFICATION_TITLES = {
    "master_booking_request_cancelled": "Клиент отменил заявку",
    "master_new_booking_request": "Новая заявка на обсуждение услуги",
    "master_new_conversation_message": "Новое сообщение клиента в заявке",
    "client_new_conversation_message": "Новое сообщение специалиста в заявке",
    "client_price_proposal": "Специалист предложил условия и цену",
    "master_price_proposal_accepted": "Клиент принял предложенные условия",
    "master_price_proposal_rejected": "Клиент отклонил предложенные условия",
    "client_booking_request_closed": "Специалист закрыл диалог по заявке",
}


def access_predicate(actor_user_id: UUID):
    membership = (
        exists()
        .where(
            BusinessMember.business_id == BookingRequest.business_id,
            BusinessMember.user_id == actor_user_id,
            BusinessMember.is_active.is_(True),
            BusinessMember.role.in_([MemberRole.MASTER.value, MemberRole.OWNER.value]),
        )
        .correlate(BookingRequest)
    )
    master = exists().where(
        Master.id == BookingRequest.master_id,
        Master.business_id == BookingRequest.business_id,
        Master.user_id == actor_user_id,
        Master.is_active.is_(True),
        membership,
    )
    return or_(BookingRequest.client_user_id == actor_user_id, master)


async def get_request(
    session: AsyncSession,
    *,
    business_id: UUID,
    request_id: UUID,
    actor_user_id: UUID,
    lock: bool = False,
) -> BookingRequest:
    statement = (
        select(BookingRequest)
        .where(
            BookingRequest.id == request_id,
            BookingRequest.business_id == business_id,
            access_predicate(actor_user_id),
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    request = await session.scalar(statement)
    if request is None:
        raise ConversationAccessError("Request is unavailable")
    return request


def require_client(request: BookingRequest, actor_user_id: UUID) -> None:
    if request.client_user_id != actor_user_id:
        raise ConversationAccessError("Only the request client can perform this action")


def require_master(request: BookingRequest, actor_user_id: UUID) -> None:
    if request.client_user_id == actor_user_id:
        raise ConversationAccessError("Only the assigned specialist can perform this action")


async def get_conversation(session: AsyncSession, request_id: UUID) -> Conversation:
    conversation = await session.scalar(
        select(Conversation)
        .where(Conversation.booking_request_id == request_id)
        .execution_options(populate_existing=True)
    )
    if conversation is None:
        raise ConversationAccessError("Conversation is unavailable")
    return conversation


async def latest_proposal(session: AsyncSession, request_id: UUID) -> PriceProposal | None:
    return await session.scalar(
        select(PriceProposal)
        .where(PriceProposal.booking_request_id == request_id)
        .order_by(PriceProposal.revision.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )


def transition(request: BookingRequest, target: BookingRequestStatus) -> None:
    check_transition(request.status, target)
    request.status = target.value


async def append_message(
    session: AsyncSession,
    *,
    conversation: Conversation,
    actor_user_id: UUID,
    sender_role: ConversationSenderRole,
    message_type: ConversationMessageType,
    text: str | None = None,
    telegram_file_id: str | None = None,
    telegram_file_unique_id: str | None = None,
    event_type: str | None = None,
    event_payload: dict[str, Any] | None = None,
) -> ConversationMessage:
    # Caller holds the request lock. A transactional counter avoids UUID/time cursor races.
    conversation.last_message_sequence += 1
    conversation.updated_at = datetime.now(UTC)
    message = ConversationMessage(
        conversation_id=conversation.id,
        sequence=conversation.last_message_sequence,
        sender_user_id=actor_user_id,
        sender_role=sender_role.value,
        message_type=message_type.value,
        text=text,
        telegram_file_id=telegram_file_id,
        telegram_file_unique_id=telegram_file_unique_id,
        event_type=event_type,
        event_payload=event_payload,
    )
    session.add(message)
    await session.flush()
    return message


async def record_event(
    session: AsyncSession,
    *,
    request: BookingRequest,
    actor_user_id: UUID,
    action: str,
    details: dict[str, Any] | None = None,
) -> ConversationMessage:
    details = details or {}
    conversation = await get_conversation(session, request.id)
    message = await append_message(
        session,
        conversation=conversation,
        actor_user_id=actor_user_id,
        sender_role=ConversationSenderRole.SYSTEM,
        message_type=ConversationMessageType.SYSTEM,
        event_type=action,
        event_payload=details,
    )
    session.add(
        AuditLog(
            business_id=request.business_id,
            actor_user_id=actor_user_id,
            action=action,
            entity_type="booking_request",
            entity_id=request.id,
            details={
                **{key: value for key, value in details.items() if key != "comment"},
                "message_id": str(message.id),
            },
        )
    )
    return message


async def enqueue_notification(
    session: AsyncSession,
    *,
    request: BookingRequest,
    kind: str,
    event_id: UUID,
) -> None:
    if kind not in REQUEST_NOTIFICATION_TITLES:
        raise ValueError("Unknown request notification kind")
    if kind.startswith("master_"):
        master = await session.get(Master, request.master_id)
        recipient = master.user_id if master is not None else None
    else:
        recipient = request.client_user_id
    if recipient is None:
        return
    session.add(
        NotificationJob(
            business_id=request.business_id,
            booking_request_id=request.id,
            recipient_user_id=recipient,
            kind=kind,
            scheduled_for=datetime.now(UTC),
            event_key=f"{kind}:{event_id}:{recipient}",
            payload={"event_id": str(event_id)},
        )
    )

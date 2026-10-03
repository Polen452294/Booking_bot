"""Transport-independent validation and request transitions."""

from booking_bot.domain.enums import BookingRequestStatus as Status
from booking_bot.domain.enums import ConversationMessageType

MAX_TEXT_LENGTH = 4000
MAX_DESCRIPTION_LENGTH = 4000
MAX_COMMENT_LENGTH = 2000
MAX_PAGE_SIZE = 100


class ConversationError(RuntimeError):
    pass


class ConversationAccessError(ConversationError):
    """Unavailable and unauthorized IDs intentionally have the same outcome."""


class InvalidRequestTransitionError(ConversationError):
    pass


class ProposalNotPendingError(ConversationError):
    pass


class ConversationClosedError(ConversationError):
    pass


TRANSITIONS = {
    Status.DRAFT: {Status.WAITING_MASTER, Status.CANCELLED, Status.CLOSED},
    Status.WAITING_MASTER: {
        Status.WAITING_CLIENT,
        Status.TERMS_PROPOSED,
        Status.CANCELLED,
        Status.CLOSED,
    },
    Status.WAITING_CLIENT: {
        Status.WAITING_MASTER,
        Status.TERMS_PROPOSED,
        Status.CANCELLED,
        Status.CLOSED,
    },
    Status.TERMS_PROPOSED: {
        Status.TERMS_ACCEPTED,
        Status.WAITING_MASTER,
        Status.CANCELLED,
        Status.CLOSED,
    },
    Status.TERMS_ACCEPTED: {Status.TERMS_PROPOSED, Status.BOOKED, Status.CANCELLED, Status.CLOSED},
    Status.BOOKED: set(),
    Status.CANCELLED: set(),
    Status.CLOSED: set(),
}


def check_transition(current: str, target: Status) -> None:
    if target not in TRANSITIONS[Status(current)]:
        raise InvalidRequestTransitionError(f"Cannot transition {current} to {target}")


def validate_text(value: str, *, maximum: int, field: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not value.strip():
        raise ValueError(f"{field} must contain 1-{maximum} characters")
    return value.strip()


def validate_message(
    message_type: ConversationMessageType,
    text: str | None,
    file_id: str | None,
    file_unique_id: str | None,
) -> tuple[ConversationMessageType, str | None]:
    kind = ConversationMessageType(message_type)
    if kind == ConversationMessageType.SYSTEM:
        raise ValueError("System events can only be created by domain operations")
    body = validate_text(text, maximum=MAX_TEXT_LENGTH, field="text") if text is not None else None
    if kind == ConversationMessageType.TEXT:
        if body is None or file_id is not None or file_unique_id is not None:
            raise ValueError("Text messages require text and cannot contain media")
    else:
        validate_text(file_id, maximum=512, field="file_id")
        validate_text(file_unique_id, maximum=256, field="file_unique_id")
    return kind, body


def validate_page(limit: int, cursor: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    if type(cursor) is not int or not 0 <= cursor <= 2_147_483_647:
        raise ValueError("cursor/offset must be a nonnegative 32-bit integer")

import pytest

from booking_bot.domain.conversations import (
    InvalidRequestTransitionError,
    check_transition,
    validate_message,
    validate_page,
    validate_text,
)
from booking_bot.domain.enums import BookingRequestStatus as Status
from booking_bot.domain.enums import ConversationMessageType as Kind
from booking_bot.specialist_config import ServiceConfig


@pytest.mark.parametrize("current", [Status.CANCELLED, Status.CLOSED, Status.BOOKED])
@pytest.mark.parametrize("target", list(Status))
def test_terminal_request_states_cannot_transition(current, target):
    with pytest.raises(InvalidRequestTransitionError):
        check_transition(current, target)


def test_request_state_machine_requires_agreement():
    for current, target in [
        (Status.DRAFT, Status.WAITING_MASTER),
        (Status.WAITING_MASTER, Status.TERMS_PROPOSED),
        (Status.TERMS_PROPOSED, Status.TERMS_ACCEPTED),
        (Status.TERMS_ACCEPTED, Status.BOOKED),
        (Status.TERMS_ACCEPTED, Status.TERMS_PROPOSED),
    ]:
        check_transition(current, target)
    with pytest.raises(InvalidRequestTransitionError):
        check_transition(Status.WAITING_MASTER, Status.BOOKED)


@pytest.mark.parametrize(
    "args",
    [
        (Kind.TEXT, None, None, None),
        (Kind.TEXT, "text", "file", "unique"),
        (Kind.PHOTO, None, None, "unique"),
        (Kind.DOCUMENT, None, "file", None),
        (Kind.SYSTEM, "injected", None, None),
        ("video", None, "file", "unique"),
        (Kind.TEXT, "a" * 4001, None, None),
    ],
)
def test_message_payload_validation(args):
    with pytest.raises(ValueError):
        validate_message(*args)


def test_valid_payloads_and_server_side_limits():
    assert validate_message(Kind.PHOTO, "caption", "file", "unique") == (Kind.PHOTO, "caption")
    assert validate_message(Kind.DOCUMENT, None, "file", "unique") == (Kind.DOCUMENT, None)
    with pytest.raises(ValueError):
        validate_text("x" * 4001, maximum=4000, field="description")
    for limit, cursor in [(101, 0), (0, 0), (10, -1), (True, 0), (10, 2**63), (10, True)]:
        with pytest.raises(ValueError):
            validate_page(limit, cursor)


def test_existing_service_config_defaults_to_fixed():
    config = ServiceConfig("key", "name", "", 60, 0, 0, 100, False)
    assert config.pricing_mode == "fixed"

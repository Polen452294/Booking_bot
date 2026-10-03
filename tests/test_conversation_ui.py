from html import unescape
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from booking_bot.bot.conversation_ui import body, cb, keyboard, render_history
from booking_bot.domain.money import format_money, parse_money


@pytest.mark.parametrize(
    "value,currency,expected",
    [
        ("15 000", "RUB", 1500000),
        ("15000,50 ₽", "RUB", 1500050),
        ("10.25 USD", "USD", 1025),
        ("0", "EUR", 0),
        ("20", "KZT", 2000),
    ],
)
def test_parse_price_in_service_currency(value, currency, expected):
    assert parse_money(value, currency) == expected


@pytest.mark.parametrize(
    "value",
    ["-1", "NaN", "1e6", "1.001", "0.001", "20000001", "15 USD", "", "free", "1$", "15000 RUB RUB"],
)
def test_reject_invalid_negative_excessive_and_wrong_currency_prices(value):
    with pytest.raises(ValueError):
        parse_money(value, "RUB")


def test_format_money_keeps_minor_units_and_configured_currency():
    assert format_money(1500050, "RUB") == "15 000,50 ₽"
    assert format_money(100, "KZT") == "1 KZT"
    assert format_money(0, "USD") == "0 $"


@pytest.mark.parametrize("action", ["view", "appointment", "chat", "yescancel", "accept", "reject"])
def test_callbacks_fit_telegram_limit(action):
    value = cb(action, uuid4(), 2147483647)
    assert len(value.encode()) <= 64
    assert keyboard(("Открыть", value)).inline_keyboard[0][0].callback_data == value


def test_system_events_and_html_are_distinct_from_user_messages():
    actor = uuid4()
    message = SimpleNamespace(
        message_type="text", sender_user_id=actor, sender_role="client", text="<script>Hi</script>"
    )
    assert body(message, actor) == "<b>Вы:</b>\n&lt;script&gt;Hi&lt;/script&gt;"
    event = SimpleNamespace(
        message_type="system",
        event_type="price_proposed",
        event_payload={
            "amount_minor": 1700000,
            "currency": "RUB",
            "previous_amount_minor": 1500000,
        },
    )
    assert "────────────" in body(event, actor)
    assert "15 000 ₽ → 17 000 ₽" in body(event, actor)


def test_appointment_event_uses_deployment_timezone_without_raw_ids():
    event = SimpleNamespace(
        message_type="system",
        event_type="appointment_created",
        event_payload={
            "starts_at": "2026-10-12T12:00:00+00:00",
            "amount_minor": 1500000,
            "currency": "RUB",
            "appointment_id": str(uuid4()),
            "proposal_id": str(uuid4()),
        },
    )
    rendered = body(event, uuid4(), "Europe/Moscow")
    assert "12.10.2026 15:00" in rendered
    assert "15 000 ₽" in rendered
    assert event.event_payload["appointment_id"] not in rendered


async def test_history_batches_short_messages_without_losing_order():
    actor = uuid4()
    target = SimpleNamespace(answer=AsyncMock())
    messages = [
        SimpleNamespace(
            message_type="text", sender_user_id=actor, sender_role="client", text=f"Message {n}"
        )
        for n in range(20)
    ]
    await render_history(target, messages, actor)
    target.answer.assert_awaited_once()
    rendered = target.answer.await_args.args[0]
    assert rendered == "\n\n".join(body(message, actor) for message in messages)


async def test_history_keeps_long_escaped_messages_complete_within_telegram_limit():
    actor = uuid4()
    target = SimpleNamespace(answer=AsyncMock())
    messages = [
        SimpleNamespace(
            message_type="text", sender_user_id=actor, sender_role="client", text=character * 4000
        )
        for character in ("<", ">")
    ]
    await render_history(target, messages, actor)
    assert target.answer.await_count == 2
    for call, message in zip(target.answer.await_args_list, messages, strict=True):
        assert call.args[0] == body(message, actor)
        assert len(unescape(call.args[0])) <= 4096

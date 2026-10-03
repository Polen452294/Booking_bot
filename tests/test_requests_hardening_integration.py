"""Commit/Telegram failure and runtime-loss checks through the real dispatcher."""

from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramNetworkError
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

import test_requests_telegram_integration as telegram
from booking_bot.bot.conversation_ui import cb
from booking_bot.db.models import Appointment, Conversation, ConversationMessage
from booking_bot.db.session import async_session_factory

pytestmark = pytest.mark.integration
business_data = telegram.business_data
ux = telegram.ux


@pytest.mark.parametrize("operation", ["create", "reply", "propose", "accept", "cancel", "book"])
async def test_failed_commit_never_shows_success_and_same_update_can_retry(
    ux, monkeypatch, operation
):
    if operation == "create":
        await ux.begin()
        await ux.send("client", text="request")
        actor, command = "client", dict(callback="rq:submit")
    else:
        await ux.create_request()
        request = await ux.request()
        if operation == "reply":
            await ux.send("client", callback=cb("reply", request.id))
            actor, command = "client", dict(text="persist me")
        elif operation == "cancel":
            actor, command = "client", dict(callback=cb("yescancel", request.id))
        else:
            await ux.send("owner", callback=cb("price", request.id))
            await ux.send("owner", text="17000")
            await ux.send("owner", callback="rq:skip")
            actor, command = "owner", dict(callback="rq:propose")
            if operation in {"accept", "book"}:
                await ux.send(actor, **command)
                proposal = await ux.proposal()
                actor, command = "client", dict(callback=cb("accept", proposal.id))
                if operation == "book":
                    await ux.send(actor, **command)
                    await ux.send("client", callback=cb("book", request.id))
                    await ux.send("client", callback=f"date:{ux.day.isoformat()}")
                    await ux.send("client", callback=ux.button("slot:"))
                    command = dict(callback="booking:confirm")
    state = ux.state(actor)
    before_state, before_data = await state.get_state(), await state.get_data()
    before_request = await ux.request()
    old_status = before_request.status if before_request else None
    async with async_session_factory() as session:
        before_count = await session.scalar(select(func.count()).select_from(ConversationMessage))
    with monkeypatch.context() as patch:
        patch.setattr(
            AsyncSession,
            "commit",
            AsyncMock(side_effect=OperationalError("COMMIT", {}, Exception())),
        )
        with pytest.raises(OperationalError):
            await ux.send(actor, **command)
    assert not ux.calls  # No success screen, toast, edit, or contact acknowledgement before commit.
    assert await state.get_state() == before_state
    assert await state.get_data() == before_data
    current = await ux.request()
    assert (current.status if current else None) == old_status
    async with async_session_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ConversationMessage))
            == before_count
        )
    payload = ux.last_payload
    await ux.send(actor, replay=payload)
    async with async_session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(ConversationMessage))
    await ux.send(actor, replay=payload)
    async with async_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ConversationMessage)) == count
    assert not ux.calls


async def test_ui_failure_after_commit_preserves_message_outbox_and_prevents_replay(
    ux, monkeypatch
):
    await ux.create_request()
    request = await ux.request()
    await ux.send("client", callback=cb("reply", request.id))
    with monkeypatch.context() as patch:

        async def fail(bot, method, **kwargs):
            raise TelegramNetworkError(method=method, message="test network outage")

        patch.setattr(ux.bot.session, "make_request", AsyncMock(side_effect=fail))
        with pytest.raises(TelegramNetworkError):
            await ux.send("client", text="committed despite UI outage")
    payload = ux.last_payload
    await ux.send("client", replay=payload)
    assert not ux.calls
    assert await ux.state("client").get_state() is None
    async with async_session_factory() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.booking_request_id == request.id)
        )
        messages = list(
            await session.scalars(
                select(ConversationMessage).where(
                    ConversationMessage.conversation_id == conversation.id,
                    ConversationMessage.text == "committed despite UI outage",
                )
            )
        )
        assert len(messages) == 1
        from booking_bot.db.models import NotificationJob

        assert (
            await session.scalar(
                select(func.count())
                .select_from(NotificationJob)
                .where(
                    NotificationJob.event_key.like(
                        f"master_new_conversation_message:{messages[0].id}:%"
                    )
                )
            )
            == 1
        )


async def test_redis_runtime_loss_preserves_history_accepted_terms_and_reopens_request(ux):
    await ux.create_request()
    request = await ux.request()
    await ux.send("owner", callback=cb("price", request.id))
    await ux.send("owner", text="17000")
    await ux.send("owner", callback="rq:skip")
    await ux.send("owner", callback="rq:propose")
    proposal = await ux.proposal()
    payload = await ux.send("client", callback=cb("accept", proposal.id))
    await ux.send("client", callback=cb("reply", request.id))
    storage = ux.dispatcher.storage
    # Delete only this test namespace; never FLUSHDB on a shared service.
    keys = [key async for key in storage.redis.scan_iter(f"{ux.settings.redis_namespace}:*")]
    for actor in ("client", "owner"):
        key = ux.state(actor).key
        keys.extend(storage.key_builder.build(key, part) for part in ("state", "data"))
    if keys:
        await storage.redis.delete(*keys)
    await ux.send("client", replay=payload)  # PostgreSQL receipt survives lost Redis dedup cache.
    assert not ux.calls
    assert (await ux.request()).status == "terms_accepted"
    assert (await ux.proposal()).status == "accepted"
    await ux.send("client", text="/start")
    await ux.send("client", callback=cb("chat", request.id))
    assert "Стоимость согласована" in ux.texts()
    await ux.send("client", callback=cb("book", request.id))
    await ux.send("client", callback=f"date:{ux.day.isoformat()}")
    await ux.send("client", callback=ux.button("slot:"))
    await ux.send("client", callback="booking:confirm")
    async with async_session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Appointment)
                .where(Appointment.booking_request_id == request.id)
            )
            == 1
        )


async def test_long_unsafe_document_filename_and_mime_are_ignored(ux):
    await ux.create_request()
    request = await ux.request()
    await ux.send("client", callback=cb("reply", request.id))
    await ux.send(
        "client",
        unsupported={
            "document": {
                "file_id": "safe-telegram-reference",
                "file_unique_id": "safe-unique-reference",
                "file_name": "../../" + "x" * 5000 + ".exe",
                "mime_type": "application/x-executable",
            }
        },
    )
    async with async_session_factory() as session:
        message = await session.scalar(
            select(ConversationMessage)
            .join(Conversation)
            .where(
                Conversation.booking_request_id == request.id,
                ConversationMessage.message_type == "document",
            )
        )
        assert message.telegram_file_id == "safe-telegram-reference"
        assert message.telegram_file_unique_id == "safe-unique-reference"
        assert message.text is None
    assert not any(type(call).__name__ in {"GetFile", "DownloadFile"} for call in ux.calls)

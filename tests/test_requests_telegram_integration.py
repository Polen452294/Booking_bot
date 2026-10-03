"""Real PostgreSQL/Redis + real aiogram routing; Telegram API is isolated."""

from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import DefaultKeyBuilder, StorageKey
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import Message
from sqlalchemy import delete, func, select

import test_conversations_integration as backend_tests
from booking_bot.bot.conversation_ui import cb
from booking_bot.bot.handlers import booking, common, master, requests
from booking_bot.bot.middlewares import DatabaseSessionMiddleware
from booking_bot.bot.states import BookingStates, RequestStates
from booking_bot.config import Settings, get_settings
from booking_bot.db.models import (
    Appointment,
    BookingRequest,
    Conversation,
    ConversationMessage,
    NotificationJob,
    PriceProposal,
    Service,
    TelegramUpdateReceipt,
    TelegramUser,
    WorkingRule,
)
from booking_bot.db.session import async_session_factory
from booking_bot.services import telegram_webhook as webhook
from booking_bot.services.conversations import ConversationService
from booking_bot.services.notification_delivery import NotificationDeliveryService

pytestmark = pytest.mark.integration
business_data = backend_tests.data


class TelegramHarness:
    def __init__(self, context, dispatcher, bot, settings):
        self.context = context
        self.dispatcher = dispatcher
        self.bot = bot
        self.settings = settings
        self.service = webhook.TelegramWebhookService(settings)
        self.calls = []
        self.telegram_ids = {}
        self.counter = 0
        self.fail_edit = False
        self.fail_history = False

    async def api(self, bot, method, **kwargs):
        from aiogram.exceptions import TelegramBadRequest

        self.calls.append(method)
        if self.fail_edit and type(method).__name__ == "EditMessageText":
            self.fail_edit = False
            raise TelegramBadRequest(method=method, message="message can't be edited")
        if self.fail_history and type(method).__name__ == "SendPhoto":
            self.fail_history = False
            raise TelegramBadRequest(method=method, message="temporary test failure")
        if type(method).__name__ == "AnswerCallbackQuery":
            return True

        def message():
            return Message.model_validate(
                {
                    "message_id": 100,
                    "date": 1700000000,
                    "chat": {"id": method.chat_id, "type": "private"},
                    "text": "screen",
                },
                context={"bot": bot},
            )

        if type(method).__name__ == "SendMediaGroup":
            return [message() for _ in method.media]
        return message()

    def state(self, actor):
        tg_id = self.telegram_ids[actor]
        return FSMContext(
            self.dispatcher.storage, StorageKey(bot_id=self.bot.id, chat_id=tg_id, user_id=tg_id)
        )

    async def send(
        self,
        actor,
        *,
        callback=None,
        text=None,
        photo=None,
        document=None,
        album=None,
        contact=None,
        replay=None,
        unsupported=None,
    ):
        self.calls.clear()
        self.counter += 1
        tg_id = self.telegram_ids[actor]
        telegram_user = {
            "id": tg_id,
            "is_bot": False,
            "first_name": "Test User",
            "username": "private_username",
        }
        message = {
            "message_id": self.counter,
            "date": 1700000000,
            "chat": {"id": tg_id, "type": "private"},
            "from": telegram_user,
        }
        payload = {"update_id": self.counter}
        if callback is not None:
            payload["callback_query"] = {
                "id": str(self.counter),
                "from": telegram_user,
                "chat_instance": "test",
                "data": callback,
                "message": {**message, "text": "previous screen"},
            }
        else:
            if text is not None:
                message["caption" if photo or document else "text"] = text
            if photo:
                message["photo"] = [
                    {
                        "file_id": photo,
                        "file_unique_id": f"unique-{photo}",
                        "width": 100,
                        "height": 100,
                    }
                ]
            if document:
                message["document"] = {"file_id": document, "file_unique_id": f"unique-{document}"}
            if album:
                message["media_group_id"] = album
            if contact:
                message["contact"] = contact
            if unsupported:
                message.update(unsupported)
            payload["message"] = message
        payload = replay or payload
        self.last_payload = payload
        async with async_session_factory() as session:
            await self.service.process(
                webhook_header_secret="test-secret", payload=payload, session=session
            )
        return payload

    def texts(self):
        return "\n".join(getattr(call, "text", "") or "" for call in self.calls)

    def buttons(self):
        return [
            button
            for call in self.calls
            if getattr(call, "reply_markup", None) and hasattr(call.reply_markup, "inline_keyboard")
            for row in call.reply_markup.inline_keyboard
            for button in row
        ]

    def button(self, prefix):
        return next(
            button.callback_data
            for button in self.buttons()
            if button.callback_data.startswith(prefix)
        )

    async def request(self):
        async with async_session_factory() as session:
            return await session.scalar(
                select(BookingRequest)
                .where(BookingRequest.business_id == self.context.business)
                .order_by(BookingRequest.created_at.desc())
                .limit(1)
            )

    async def proposal(self):
        async with async_session_factory() as session:
            return await session.scalar(
                select(PriceProposal)
                .join(BookingRequest)
                .where(BookingRequest.business_id == self.context.business)
                .order_by(PriceProposal.revision.desc())
                .limit(1)
            )

    async def begin(self, mode="negotiable"):
        async with async_session_factory() as session, session.begin():
            service = await session.get(Service, self.context.service)
            service.pricing_mode = mode
        await self.send("client", callback="booking:start")
        await self.send("client", callback=f"service:{self.context.service}")

    async def create_request(self):
        await self.begin()
        await self.send("client", text="Индивидуальный эскиз <script>")
        await self.send("client", callback="rq:submit")
        return await self.request()


@pytest_asyncio.fixture(loop_scope="session")
async def ux(business_data, monkeypatch):
    data = business_data
    settings = Settings(
        _env_file=None,
        telegram_bot_token=f"{uuid4().int % 10**10}:test-token",
        telegram_webhook_header_secret="test-secret",
    )
    prefix = f"telegram-ux:{uuid4().hex}"
    storage = RedisStorage.from_url(
        get_settings().redis_url,
        key_builder=DefaultKeyBuilder(prefix=prefix, with_bot_id=True),
        state_ttl=120,
        data_ttl=120,
    )
    dispatcher = Dispatcher(storage=storage, events_isolation=storage.create_isolation())
    dispatcher.update.outer_middleware(DatabaseSessionMiddleware())
    # Router instances in the app are attached already. Copy registrations, not router ownership.
    for original in (common.router, master.router, requests.router, booking.router):
        router = Router()
        for name in ("message", "callback_query"):
            router.observers[name].handlers.extend(original.observers[name].handlers)
        dispatcher.include_router(router)
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    harness = TelegramHarness(data, dispatcher, bot, settings)
    monkeypatch.setattr(bot.session, "make_request", AsyncMock(side_effect=harness.api))
    monkeypatch.setattr(webhook, "dispatcher", dispatcher)
    monkeypatch.setattr(webhook, "create_telegram_bot", lambda *_: bot)
    context = SimpleNamespace(
        business_id=data.business,
        master_id=data.master,
        master=SimpleNamespace(display_name="Master", bio=""),
    )
    monkeypatch.setattr(webhook, "get_specialist_context", AsyncMock(return_value=context))
    monkeypatch.setattr(common, "get_specialist_context", AsyncMock(return_value=context))
    async with async_session_factory() as session, session.begin():
        for actor in ("client", "owner", "stranger"):
            user = await session.get(TelegramUser, getattr(data, actor))
            harness.telegram_ids[actor] = user.telegram_user_id
        # Keep time selection independent of the machine's date.
        day = datetime.now(UTC).astimezone(ZoneInfo("Europe/Moscow")).date() + timedelta(days=2)
        session.add(
            WorkingRule(
                business_id=data.business,
                master_id=data.master,
                weekday=day.weekday(),
                start_time=time(9),
                end_time=time(18),
            )
        )
        harness.day = day
    yield harness
    keys = [key async for key in storage.redis.scan_iter(f"{prefix}:*")]
    keys.extend([key async for key in storage.redis.scan_iter(f"{settings.redis_namespace}:*")])
    if keys:
        await storage.redis.delete(*keys)
    await dispatcher.fsm.close()
    await bot.session.close()
    async with async_session_factory() as session, session.begin():
        await session.execute(
            delete(TelegramUpdateReceipt).where(
                TelegramUpdateReceipt.namespace == settings.redis_namespace
            )
        )


async def test_full_telegram_request_price_booking_and_continued_conversation(ux):
    await ux.begin()
    assert await ux.state("client").get_state() == RequestStates.collecting.state
    assert "date:" not in str(ux.buttons())
    await ux.send("client", text="Хочу индивидуальный эскиз 12×8 см")
    await ux.send("client", photo="reference-photo", text="Референс")
    await ux.send("client", document="reference-document")
    await ux.send("client", callback="rq:submit")
    request = await ux.request()
    assert request.status == "waiting_master"
    assert await ux.state("client").get_state() is None
    async with async_session_factory() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.booking_request_id == request.id)
        )
        messages = list(
            await session.scalars(
                select(ConversationMessage)
                .where(ConversationMessage.conversation_id == conversation.id)
                .order_by(ConversationMessage.sequence)
            )
        )
        assert [m.message_type for m in messages].count("photo") == 1
        assert [m.message_type for m in messages].count("document") == 1
        assert any(m.telegram_file_id == "reference-photo" for m in messages)
        job = await session.scalar(
            select(NotificationJob).where(NotificationJob.booking_request_id == request.id)
        )
        assert job.kind == "master_new_booking_request"
        notification = await NotificationDeliveryService(Settings(_env_file=None))._build_payload(
            session, job
        )
        assert notification.reply_markup.inline_keyboard[0][0].callback_data == cb(
            "view", request.id
        )
    await ux.send("client", callback="rq:mine")
    assert {"Активные", "Завершённые"} <= {b.text for b in ux.buttons()}
    await ux.send("client", callback="rq:list:c:active:0")
    assert ux.button("rq:view:") == cb("view", request.id)
    await ux.send("owner", callback="master:menu")
    assert any("Заявки и диалоги 🔴" in b.text for b in ux.buttons())
    await ux.send("owner", callback="rq:inbox")
    await ux.send("owner", callback="rq:list:m:new:0")
    assert ux.button("rq:view:") == cb("view", request.id)
    await ux.send("owner", callback=cb("view", request.id))
    assert request.client_phone_snapshot in ux.texts()
    assert "private_username" not in ux.texts()
    await ux.send("owner", callback=cb("chat", request.id))
    async with async_session_factory() as session:
        assert (
            await ConversationService().unread_count(
                session,
                business_id=ux.context.business,
                conversation_id=conversation.id,
                actor_user_id=ux.context.owner,
            )
            == 0
        )
    await ux.send("owner", callback="rq:list:m:new:0")
    assert "Заявок пока нет" in ux.texts()
    await ux.send("owner", callback=cb("reply", request.id))
    await ux.send("owner", text="Пришлите размеры и место нанесения")
    assert (await ux.request()).status == "waiting_client"
    await ux.send("owner", callback=cb("price", request.id))
    await ux.send("owner", text="15000")
    await ux.send("owner", text="Цена включает разработку эскиза")
    assert await ux.proposal() is None  # confirmation precedes the domain operation
    await ux.send("owner", callback="rq:propose")
    proposal = await ux.proposal()
    assert proposal.amount_minor == 1_500_000 and proposal.status == "pending"
    await ux.send("client", callback=cb("chat", request.id))
    assert "💰 Мастер предложил стоимость: 15 000 ₽" in ux.texts()
    await ux.send("client", callback=cb("reply", request.id))
    await ux.send("client", text="Подходит")
    assert (await ux.request()).status == "terms_proposed"
    await ux.send("owner", callback=cb("price", request.id))
    await ux.send("owner", text="17000")
    await ux.send("owner", callback="rq:skip")
    await ux.send("owner", callback="rq:propose")
    await ux.send("client", callback=cb("accept", proposal.id))
    assert "неактуально" in ux.texts()
    assert (await ux.request()).status == "terms_proposed"
    proposal = await ux.proposal()
    await ux.send("client", callback=cb("accept", proposal.id))
    assert (await ux.request()).status == "terms_accepted"
    await ux.send("client", callback=cb("book", request.id))
    assert await ux.state("client").get_state() == BookingStates.selecting_date.state
    await ux.send("client", callback=f"date:{ux.day.isoformat()}")
    selected_slot = ux.button("slot:")
    await ux.send("client", callback=selected_slot)
    payload = await ux.send("client", callback="booking:confirm")
    assert (await ux.request()).status == "booked"
    async with async_session_factory() as session:
        appointment = await session.scalar(
            select(Appointment).where(Appointment.booking_request_id == request.id)
        )
        assert appointment.price_minor == 1_700_000 and appointment.currency == "RUB"
    await ux.send("client", replay=payload)
    async with async_session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Appointment)
                .where(Appointment.booking_request_id == request.id)
            )
            == 1
        )
    for actor in ("owner", "client"):
        await ux.send(actor, callback=cb("appt", request.id))
        assert any(b.callback_data == cb("appointment", appointment.id) for b in ux.buttons())
        await ux.send(actor, callback=cb("appointment", appointment.id))
        assert "✅ Запись создана" in ux.texts()
        await ux.send(actor, callback=cb("reply", request.id))
        await ux.send(actor, text="Сообщение после записи")
    assert (await ux.request()).status == "booked"
    await ux.send("client", callback="rq:list:c:done:0")
    assert ux.button("rq:view:") == cb("view", request.id)
    from requests_backup_support import verify_e2e_dump_restore

    await verify_e2e_dump_restore(ux.context.business)


@pytest.mark.parametrize(
    "mode,choice,expected",
    [
        ("fixed", None, BookingStates.selecting_date.state),
        ("from", "book", BookingStates.selecting_date.state),
        ("from", "discuss", RequestStates.collecting.state),
    ],
)
async def test_pricing_modes_preserve_fixed_and_offer_from_choice(ux, mode, choice, expected):
    await ux.begin(mode)
    if choice:
        assert "Цена: от" in ux.texts()
        await ux.send("client", callback=f"rq:from:{choice}")
    assert await ux.state("client").get_state() == expected
    assert await ux.request() is None


async def test_price_replace_stale_accept_discuss_reject_and_edit_fallback(ux):
    request = await ux.create_request()
    for amount in (15000, 17000):
        await ux.send("owner", callback=cb("price", request.id))
        await ux.send("owner", text=str(amount))
        await ux.send("owner", callback="rq:skip")
        await ux.send("owner", callback="rq:propose")
        if amount == 15000:
            old = await ux.proposal()
    proposal = await ux.proposal()
    await ux.send("client", callback=cb("accept", old.id))
    assert "уже неактуально" in ux.texts()
    assert (await ux.request()).status == "terms_proposed"
    await ux.send("client", callback=cb("offer", request.id))
    assert "17 000 ₽" in ux.texts()
    await ux.send("client", callback=cb("chat", request.id))
    assert "15 000 ₽ → 17 000 ₽" in ux.texts()
    assert (await ux.proposal()).status == "pending"  # Discuss does not reject.
    ux.fail_edit = True
    await ux.send("client", callback=cb("accept", proposal.id))
    assert (await ux.request()).status == "terms_accepted"
    assert any(type(c).__name__ == "SendMessage" for c in ux.calls)
    await ux.send("client", callback=cb("accept", proposal.id))
    assert "уже неактуально" in ux.texts()
    await ux.send("owner", callback=cb("price", request.id))
    await ux.send("owner", text="18000")
    await ux.send("owner", callback="rq:skip")
    await ux.send("owner", callback="rq:propose")
    await ux.send("client", callback=cb("reject", (await ux.proposal()).id))
    assert (await ux.request()).status == "waiting_master"


async def test_callbacks_enforce_ownership_and_master_role(ux):
    request = await ux.create_request()
    await ux.send("owner", callback=cb("price", request.id))
    await ux.send("owner", text="1000")
    await ux.send("owner", callback="rq:skip")
    await ux.send("owner", callback="rq:propose")
    proposal = await ux.proposal()
    for callback in (
        cb("view", request.id),
        cb("chat", request.id),
        cb("reply", request.id),
        cb("price", request.id),
        cb("book", request.id),
        cb("yescancel", request.id),
        cb("accept", proposal.id),
        "rq:inbox",
        "rq:list:m:reply:0",
    ):
        await ux.send("stranger", callback=callback)
        assert "Заявка недоступна" in ux.texts()
        assert "private_username" not in ux.texts()
    await ux.send("client", callback=cb("price", request.id))
    assert "Заявка недоступна" in ux.texts()
    assert (await ux.proposal()).status == "pending"


@pytest.mark.parametrize("command", ["/start", "/cancel"])
@pytest.mark.parametrize("input_state", ["request", "reply", "price", "comment", "confirm"])
async def test_commands_leave_request_reply_and_price_fsm(ux, command, input_state):
    actor = "client"
    if input_state == "request":
        await ux.begin()
    else:
        request = await ux.create_request()
        actor = "owner" if input_state in {"price", "comment", "confirm"} else "client"
        await ux.send(
            actor, callback=cb("reply" if input_state == "reply" else "price", request.id)
        )
        if input_state in {"comment", "confirm"}:
            await ux.send(actor, text="15000")
        if input_state == "confirm":
            await ux.send(actor, callback="rq:skip")
    await ux.send(actor, text=command)
    assert await ux.state(actor).get_state() is None
    async with async_session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ConversationMessage)
                .where(ConversationMessage.text == command)
            )
            == 0
        )


async def test_media_albums_limits_unsupported_and_cancel_input(ux):
    await ux.begin()
    await ux.send("client", text="x" * 4001)
    assert "Максимум 4000" in ux.texts()
    assert "description" not in await ux.state("client").get_data()
    await ux.send(
        "client",
        unsupported={
            "video": {
                "file_id": "video",
                "file_unique_id": "v",
                "width": 100,
                "height": 100,
                "duration": 5,
            }
        },
    )
    assert "текст, фотографию или документ" in ux.texts()
    for n in range(3):
        await ux.send("client", photo=f"album-{n}", album="album-1")
        assert not ux.calls
    await ux.send("client", callback="rq:submit")
    request = await ux.request()
    await ux.send("owner", callback=cb("chat", request.id))
    albums = [c for c in ux.calls if type(c).__name__ == "SendMediaGroup"]
    assert len(albums) == 1 and len(albums[0].media) == 3
    await ux.send("owner", callback=cb("reply", request.id))
    for n in range(2):
        await ux.send("owner", photo=f"reply-photo-{n}", album="album-2")
        assert not ux.calls
    assert await ux.state("owner").get_state() == RequestStates.reply.state
    await ux.send("owner", callback=cb("view", request.id))
    assert await ux.state("owner").get_state() is None
    await ux.send("client", callback=cb("chat", request.id))
    assert any(type(c).__name__ == "SendMediaGroup" for c in ux.calls)
    await ux.send("client", callback=cb("reply", request.id))
    await ux.send("client", callback=cb("view", request.id))
    assert await ux.state("client").get_state() is None


async def test_client_contact_is_collected_after_description_and_before_submit(ux):
    async with async_session_factory() as session, session.begin():
        user = await session.get(TelegramUser, ux.context.client)
        user.phone = None
    await ux.begin()
    await ux.send("client", text="Описание задачи")
    await ux.send("client", callback="rq:submit")
    assert await ux.state("client").get_state() == RequestStates.phone.state
    assert await ux.request() is None
    await ux.send(
        "client",
        contact={
            "phone_number": "+79991112233",
            "first_name": "Other",
            "user_id": ux.telegram_ids["stranger"],
        },
    )
    assert "свой контакт" in ux.texts()
    await ux.send("client", text="79991112233")
    assert (await ux.request()).client_phone_snapshot == "+79991112233"


@pytest.mark.parametrize("actor,action", [("client", "cancel"), ("owner", "close")])
async def test_confirmed_close_cancel_preserves_history_and_blocks_writes(ux, actor, action):
    request = await ux.create_request()
    await ux.send(actor, callback=cb(action, request.id))
    assert (await ux.request()).status == "waiting_master"
    await ux.send(actor, callback=cb("yes" + action, request.id))
    assert (await ux.request()).status == ("cancelled" if action == "cancel" else "closed")
    await ux.send("client", callback=cb("reply", request.id))
    assert "Диалог закрыт" in ux.texts()
    await ux.send("owner", callback=cb("chat", request.id))
    assert "История сохранена" in ux.texts()
    if action == "cancel":
        async with async_session_factory() as session:
            assert (
                await session.scalar(
                    select(NotificationJob.id).where(
                        NotificationJob.booking_request_id == request.id,
                        NotificationJob.kind == "master_booking_request_cancelled",
                    )
                )
                is not None
            )


async def test_failed_history_delivery_does_not_mark_messages_read(ux):
    from aiogram.exceptions import TelegramBadRequest

    request = await ux.create_request()
    await ux.send("client", callback=cb("reply", request.id))
    await ux.send("client", photo="history-photo")
    async with async_session_factory() as session:
        conversation = await requests.CHAT.get(
            session,
            business_id=ux.context.business,
            request_id=request.id,
            actor_user_id=ux.context.owner,
        )
        before = await requests.CHAT.unread_count(
            session,
            business_id=ux.context.business,
            conversation_id=conversation.id,
            actor_user_id=ux.context.owner,
        )
    ux.fail_history = True
    with pytest.raises(TelegramBadRequest):
        await ux.send("owner", callback=cb("chat", request.id))
    async with async_session_factory() as session:
        assert (
            await requests.CHAT.unread_count(
                session,
                business_id=ux.context.business,
                conversation_id=conversation.id,
                actor_user_id=ux.context.owner,
            )
            == before
        )
    await ux.send("owner", callback=cb("chat", request.id))
    async with async_session_factory() as session:
        assert (
            await requests.CHAT.unread_count(
                session,
                business_id=ux.context.business,
                conversation_id=conversation.id,
                actor_user_id=ux.context.owner,
            )
            == 0
        )


async def test_slot_conflict_keeps_accepted_request_proposal_and_history(ux):
    from booking_bot.services.bookings import BookingService

    request = await ux.create_request()
    await ux.send("owner", callback=cb("price", request.id))
    await ux.send("owner", text="15000")
    await ux.send("owner", callback="rq:skip")
    await ux.send("owner", callback="rq:propose")
    proposal = await ux.proposal()
    await ux.send("client", callback=cb("accept", proposal.id))
    await ux.send("client", callback=cb("book", request.id))
    await ux.send("client", callback=f"date:{ux.day.isoformat()}")
    slot = ux.button("slot:")
    start = datetime.fromtimestamp(int(slot.split(":")[1]), tz=UTC)
    async with async_session_factory() as session, session.begin():
        await BookingService(get_settings()).create_hold(
            session,
            business_id=ux.context.business,
            master_id=ux.context.master,
            service_id=ux.context.service,
            client_id=ux.context.stranger,
            service_start=start,
            local_date=ux.day,
        )
    await ux.send("client", callback=slot)
    assert "уже занято" in ux.texts()
    assert (await ux.request()).status == "terms_accepted"
    assert (await ux.proposal()).status == "accepted"
    await ux.send("client", callback=cb("chat", request.id))
    assert "Индивидуальный эскиз" in ux.texts()


async def test_proposal_stays_committed_when_notification_temporarily_fails(ux):
    request = await ux.create_request()
    await ux.send("owner", callback=cb("price", request.id))
    await ux.send("owner", text="0")
    await ux.send("owner", callback="rq:skip")
    await ux.send("owner", callback="rq:propose")
    worker = NotificationDeliveryService(Settings(_env_file=None))
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError))
    await worker.run_once(bot, business_id=ux.context.business)
    proposal = await ux.proposal()
    assert proposal.status == "pending" and proposal.amount_minor == 0
    async with async_session_factory() as session:
        job = await session.scalar(
            select(NotificationJob).where(
                NotificationJob.booking_request_id == request.id,
                NotificationJob.kind == "client_price_proposal",
            )
        )
        assert job.state == "pending" and job.attempt_count == 1
        assert job.last_error == "TimeoutError"


async def test_double_accept_is_serialized_in_domain(ux):
    import asyncio

    from booking_bot.domain.conversations import ProposalNotPendingError

    request = await ux.create_request()
    async with async_session_factory() as session, session.begin():
        proposal = await requests.PRICES.propose(
            session,
            business_id=ux.context.business,
            request_id=request.id,
            actor_user_id=ux.context.owner,
            amount_minor=100,
        )
    gate = asyncio.Event()

    async def accept():
        await gate.wait()
        async with async_session_factory() as session, session.begin():
            try:
                await requests.PRICES.decide_by_id(
                    session,
                    business_id=ux.context.business,
                    actor_user_id=ux.context.client,
                    proposal_id=proposal.id,
                    accept=True,
                )
                return "accepted"
            except ProposalNotPendingError:
                return "stale"

    tasks = [asyncio.create_task(accept()) for _ in range(2)]
    gate.set()
    assert sorted(await asyncio.wait_for(asyncio.gather(*tasks), 10)) == ["accepted", "stale"]
    async with async_session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(NotificationJob)
                .where(
                    NotificationJob.booking_request_id == request.id,
                    NotificationJob.kind == "master_price_proposal_accepted",
                )
            )
            == 1
        )


async def test_previous_history_pagination_and_unread_on_actual_open_only(ux):
    request = await ux.create_request()
    async with async_session_factory() as session, session.begin():
        conversation = await requests.CHAT.get(
            session,
            business_id=ux.context.business,
            request_id=request.id,
            actor_user_id=ux.context.client,
        )
        for n in range(26):
            await requests.CHAT.send_message(
                session,
                business_id=ux.context.business,
                conversation_id=conversation.id,
                actor_user_id=ux.context.client,
                text=f"message-{n}",
            )
    await ux.send("owner", callback=cb("view", request.id))
    async with async_session_factory() as session:
        unread_before = await requests.CHAT.unread_count(
            session,
            business_id=ux.context.business,
            conversation_id=conversation.id,
            actor_user_id=ux.context.owner,
        )
        assert unread_before >= 26
    await ux.send("owner", callback=cb("chat", request.id))
    assert "message-0\n" not in ux.texts() and "message-25" in ux.texts()
    previous = ux.button("rq:chat:")
    await ux.send("owner", callback=previous)
    assert "message-0" in ux.texts() and "message-25" not in ux.texts()
    async with async_session_factory() as session:
        assert (
            await requests.CHAT.unread_count(
                session,
                business_id=ux.context.business,
                conversation_id=conversation.id,
                actor_user_id=ux.context.owner,
            )
            == 0
        )

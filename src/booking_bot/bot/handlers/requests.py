"""Telegram adapter for request/conversation services; no direct Telegram relay."""

from functools import wraps
from html import escape
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from booking_bot.bot.conversation_ui import (
    STATUS_LABELS,
    body,
    cb,
    edit_or_answer,
    keyboard,
    render_history,
)
from booking_bot.bot.keyboards import main_menu_keyboard, phone_keyboard
from booking_bot.bot.middlewares import persist_before_response
from booking_bot.bot.states import BookingStates, RequestStates
from booking_bot.db.models import Business, Service
from booking_bot.domain.conversations import (
    MAX_COMMENT_LENGTH,
    MAX_DESCRIPTION_LENGTH,
    MAX_TEXT_LENGTH,
    ConversationAccessError,
    ConversationClosedError,
    InvalidRequestTransitionError,
    ProposalNotPendingError,
)
from booking_bot.domain.enums import BookingRequestStatus as Status
from booking_bot.domain.enums import ConversationMessageType
from booking_bot.domain.money import format_money, parse_money
from booking_bot.services.booking_requests import BookingRequestService
from booking_bot.services.conversation_context import require_master
from booking_bot.services.conversations import ConversationService
from booking_bot.services.master_access import get_master_for_user
from booking_bot.services.price_proposals import PriceProposalService
from booking_bot.services.users import get_or_create_telegram_user, normalize_phone, set_user_phone

router = Router(name="requests")
REQUESTS = BookingRequestService()
CHAT = ConversationService()
PRICES = PriceProposalService()
PAGE_SIZE = 20
ACTIVE = [
    Status.WAITING_MASTER,
    Status.WAITING_CLIENT,
    Status.TERMS_PROPOSED,
    Status.TERMS_ACCEPTED,
]
SECTIONS = {
    "active": ACTIVE,
    "done": [Status.BOOKED, Status.CANCELLED, Status.CLOSED],
    "new": [Status.WAITING_MASTER],
    "reply": [Status.WAITING_MASTER],
    "client": [Status.WAITING_CLIENT, Status.TERMS_PROPOSED],
    "agreed": [Status.TERMS_ACCEPTED],
    "closed": [Status.BOOKED, Status.CANCELLED, Status.CLOSED],
}


def guarded(function):
    @wraps(function)
    async def wrapper(event, *args, **kwargs):
        try:
            return await function(event, *args, **kwargs)
        except ProposalNotPendingError:
            text = (
                "Это предложение уже неактуально. "
                "Откройте последнее предложение мастера в «Мои заявки»."
            )
        except ConversationAccessError:
            text = "Заявка недоступна."
        except ConversationClosedError:
            text = "Диалог закрыт."
        except InvalidRequestTransitionError:
            text = "Состояние заявки изменилось. Откройте её заново в «Мои заявки»."
        except (ValueError, KeyError, IndexError, OverflowError):
            text = "Некорректные или устаревшие данные. Откройте заявку заново."
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)

    return wrapper


def input_keyboard():
    return keyboard(
        ("Добавить фото/файл", "rq:attach"),
        ("Отправить заявку", "rq:submit"),
        ("Отмена", "rq:abort"),
    )


async def begin_request(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_data(
        {"service_id": data["service_id"], "master_id": data["master_id"], "attachments": []}
    )
    await state.set_state(RequestStates.collecting)
    await edit_or_answer(
        callback,
        "Расскажите, что вы хотите сделать.\n\nМожно отправить текст, фотографии или документы.",
        reply_markup=input_keyboard(),
    )


def media_payload(message: Message) -> dict | None:
    if message.photo:
        file = message.photo[-1]
        return {
            "message_type": ConversationMessageType.PHOTO.value,
            "text": message.caption,
            "telegram_file_id": file.file_id,
            "telegram_file_unique_id": file.file_unique_id,
        }
    if message.document:
        return {
            "message_type": ConversationMessageType.DOCUMENT.value,
            "text": message.caption,
            "telegram_file_id": message.document.file_id,
            "telegram_file_unique_id": message.document.file_unique_id,
        }
    if message.text and not message.text.startswith("/"):
        return {"message_type": ConversationMessageType.TEXT.value, "text": message.text}
    return None


async def _actor(event, session):
    if event.from_user is None:
        raise ConversationAccessError("Missing actor")
    return await get_or_create_telegram_user(session, event.from_user)


async def _context(event, session, business_id, request_id):
    user = await _actor(event, session)
    request = await REQUESTS.get(
        session, business_id=business_id, request_id=request_id, actor_user_id=user.id
    )
    return user, request


async def show_card(callback, state, session, business_id, request_id):
    user, request = await _context(callback, session, business_id, request_id)
    conversation = await CHAT.get(
        session, business_id=business_id, request_id=request_id, actor_user_id=user.id
    )
    unread = await CHAT.unread_count(
        session, business_id=business_id, conversation_id=conversation.id, actor_user_id=user.id
    )
    proposals = await PRICES.list_proposals(
        session, business_id=business_id, request_id=request_id, actor_user_id=user.id, limit=1
    )
    latest = proposals[0] if proposals else None
    price = format_money(latest.amount_minor, latest.currency) if latest else "обсуждается"
    master = request.client_user_id != user.id
    business = await session.get(Business, business_id)
    created = request.created_at.astimezone(ZoneInfo(business.timezone))
    text = (
        f"<b>{escape(request.service_name_snapshot)}</b>\n\n"
        f"Статус: {STATUS_LABELS[request.status]}\nЦена: {price}\n"
        f"Создано: {created:%d.%m.%Y %H:%M}"
    )
    if master:
        # Long descriptions are shown separately to stay within Telegram's message limit.
        text += (
            f"\nКлиент: {escape(request.client_name_snapshot)}"
            f"\nКонтакт: {escape(request.client_phone_snapshot)}"
        )
    recent = await CHAT.list_messages(
        session,
        business_id=business_id,
        conversation_id=conversation.id,
        actor_user_id=user.id,
        limit=1,
        after_sequence=max(0, conversation.last_message_sequence - 1),
    )
    if recent:
        preview = body(recent[0], user.id)
        if len(preview) < 600:
            text += f"\n\nПоследнее сообщение:\n{preview}"
        else:
            text += "\n\nПоследнее сообщение доступно в диалоге."
    if unread:
        text += f"\n\n🔴 {unread} новых сообщений"
    buttons = [("Открыть диалог", cb("chat", request_id))]
    if request.status in {status.value for status in ACTIVE}:
        if master:
            buttons.append(
                ("Изменить предложение" if latest else "Предложить цену", cb("price", request_id))
            )
        elif latest and latest.status == "pending":
            buttons.append(("Посмотреть предложение", cb("offer", request_id)))
        if not master and request.status == Status.TERMS_ACCEPTED.value:
            buttons.append(("Выбрать дату и время", cb("book", request_id)))
        buttons.append(
            (
                "Закрыть заявку" if master else "Отменить заявку",
                cb("close" if master else "cancel", request_id),
            )
        )
    if request.status == Status.BOOKED.value:
        buttons.append(("Перейти к записи", cb("appt", request_id)))
        if master and conversation.status == "open":
            buttons.append(("Закрыть диалог", cb("close", request_id)))
    buttons.extend([("Назад", "rq:inbox" if master else "rq:mine"), ("Главное меню", "menu:home")])
    await state.clear()
    await edit_or_answer(callback, text, reply_markup=keyboard(*buttons))
    if master and isinstance(callback.message, Message):
        await callback.message.answer(f"Описание задачи:\n{escape(request.description)}")


async def show_conversation(callback, state, session, business_id, request_id, upper=None):
    user, request = await _context(callback, session, business_id, request_id)
    conversation = await CHAT.get(
        session, business_id=business_id, request_id=request_id, actor_user_id=user.id
    )
    end = conversation.last_message_sequence if upper is None else upper
    if not 0 <= end <= conversation.last_message_sequence:
        raise ValueError("Invalid history cursor")
    messages = await CHAT.list_messages(
        session,
        business_id=business_id,
        conversation_id=conversation.id,
        actor_user_id=user.id,
        after_sequence=max(0, end - PAGE_SIZE),
        limit=min(PAGE_SIZE, end) or 1,
    )
    await state.clear()
    if isinstance(callback.message, Message):
        business = await session.get(Business, business_id)
        await render_history(callback.message, messages, user.id, business.timezone)
        # Mark only after all displayed history has been successfully delivered.
        if messages:
            await CHAT.mark_read(
                session,
                business_id=business_id,
                conversation_id=conversation.id,
                actor_user_id=user.id,
                through_sequence=messages[-1].sequence,
            )
        buttons = []
        if end > PAGE_SIZE:
            buttons.append(("Показать предыдущие", cb("chat", request_id, end - PAGE_SIZE)))
        if upper is not None:
            buttons.append(("Последние сообщения", cb("chat", request_id)))
        if conversation.status == "open":
            buttons.append(
                (
                    "Написать мастеру" if request.client_user_id == user.id else "Ответить",
                    cb("reply", request_id),
                )
            )
        buttons.extend([("Назад к заявке", cb("view", request_id)), ("Главное меню", "menu:home")])
        await callback.message.answer(
            "Диалог по заявке"
            if conversation.status == "open"
            else "Диалог закрыт. История сохранена.",
            reply_markup=keyboard(*buttons),
        )


@router.callback_query(BookingStates.choosing_pricing_flow, F.data.startswith("rq:from:"))
@guarded
async def from_choice(
    callback: CallbackQuery, state: FSMContext, db_session: AsyncSession, business_id: UUID
):
    if callback.data == "rq:from:book":
        from booking_bot.bot.handlers.booking import _show_dates

        await _show_dates(callback, state, db_session, business_id)
    elif callback.data == "rq:from:discuss":
        await begin_request(callback, state)
        await callback.answer()
    else:
        raise ValueError("Invalid choice")


@router.message(RequestStates.collecting)
@guarded
async def collect_request(message: Message, state: FSMContext):
    payload = media_payload(message)
    if payload is None:
        await message.answer("Можно отправить текст, фотографию или документ.")
        return
    if len(payload.get("text") or "") > MAX_DESCRIPTION_LENGTH:
        await message.answer(
            f"Сообщение слишком длинное. Максимум {MAX_DESCRIPTION_LENGTH} символов."
        )
        return
    data = await state.get_data()
    attachments = list(data.get("attachments", []))
    if not data.get("description"):
        description = payload.get("text") or "Фото/документ с описанием задачи"
        await state.update_data(description=description)
        if payload["message_type"] == "text":
            payload = None  # create() stores the description as the first message.
    if payload:
        if len(attachments) >= 30:
            await message.answer("В одной заявке можно добавить до 30 сообщений/файлов.")
            return
        attachments.append(payload)
    await state.update_data(attachments=attachments)
    # Albums generate multiple updates. A single explicit finish button handles all items.
    if not message.media_group_id:
        await message.answer(
            "Описание сохранено. При желании добавьте фото/файл или отправьте заявку.",
            reply_markup=input_keyboard(),
        )


async def submit_request(target, state, session, business_id):
    user = await _actor(target, session)
    data = await state.get_data()
    if not data.get("description"):
        raise ValueError("Description required")
    if not user.phone:
        await state.set_state(RequestStates.phone)
        message = target.message if isinstance(target, CallbackQuery) else target
        if isinstance(message, Message):
            await message.answer(
                "Отправьте номер телефона для заявки. /cancel — отменить.",
                reply_markup=phone_keyboard(),
            )
        return
    async with session.begin_nested():
        request = await REQUESTS.create(
            session,
            business_id=business_id,
            master_id=UUID(data["master_id"]),
            service_id=UUID(data["service_id"]),
            actor_user_id=user.id,
            description=data["description"],
            submit=False,
        )
        conversation = await CHAT.get(
            session, business_id=business_id, request_id=request.id, actor_user_id=user.id
        )
        for payload in data.get("attachments", []):
            await CHAT.send_message(
                session,
                business_id=business_id,
                conversation_id=conversation.id,
                actor_user_id=user.id,
                **payload,
            )
        await REQUESTS.submit(
            session, business_id=business_id, request_id=request.id, actor_user_id=user.id
        )
    await state.clear()
    await persist_before_response(session, state)
    buttons = keyboard(
        ("Открыть заявку", cb("view", request.id)),
        ("Открыть диалог", cb("chat", request.id)),
        ("Главное меню", "menu:home"),
    )
    if isinstance(target, CallbackQuery):
        await edit_or_answer(target, "✅ Заявка отправлена мастеру.", reply_markup=buttons)
    else:
        await target.answer("✅ Заявка отправлена мастеру.", reply_markup=buttons)


@router.message(RequestStates.phone)
@guarded
async def request_phone(
    message: Message, state: FSMContext, db_session: AsyncSession, business_id: UUID
):
    user = await _actor(message, db_session)
    contact = message.contact
    if contact and contact.user_id != message.from_user.id:
        await message.answer("Отправьте свой контакт или введите телефон текстом.")
        return
    phone = normalize_phone(contact.phone_number if contact else message.text or "")
    if phone is None:
        await message.answer("Введите телефон из 10–15 цифр.")
        return
    merged = await set_user_phone(db_session, user, phone)
    from booking_bot.bot.handlers.booking import _settings
    from booking_bot.services.bookings import BookingService

    await BookingService(_settings()).schedule_client_reminders_for_appointments(
        db_session, appointment_ids=merged
    )
    await submit_request(message, state, db_session, business_id)
    await message.answer("Контакт сохранён.", reply_markup=ReplyKeyboardRemove())


@router.message(RequestStates.reply)
@guarded
async def reply_message(
    message: Message, state: FSMContext, db_session: AsyncSession, business_id: UUID
):
    payload = media_payload(message)
    if payload is None:
        await message.answer("Можно отправить текст, фотографию или документ.")
        return
    if len(payload.get("text") or "") > MAX_TEXT_LENGTH:
        await message.answer(f"Сообщение слишком длинное. Максимум {MAX_TEXT_LENGTH} символов.")
        return
    data = await state.get_data()
    user, request = await _context(message, db_session, business_id, UUID(data["request_id"]))
    conversation = await CHAT.get(
        db_session, business_id=business_id, request_id=request.id, actor_user_id=user.id
    )
    await CHAT.send_message(
        db_session,
        business_id=business_id,
        conversation_id=conversation.id,
        actor_user_id=user.id,
        **payload,
    )
    if message.media_group_id:
        # Keep this explicit input state until Done so no album items get lost.
        return
    await state.clear()
    await persist_before_response(db_session, state)
    await message.answer(
        "Сообщение отправлено.",
        reply_markup=keyboard(
            ("Продолжить диалог", cb("reply", request.id)),
            ("Открыть диалог", cb("chat", request.id)),
            ("Главное меню", "menu:home"),
        ),
    )


@router.message(RequestStates.price)
@guarded
async def price_input(
    message: Message, state: FSMContext, db_session: AsyncSession, business_id: UUID
):
    data = await state.get_data()
    user, request = await _context(message, db_session, business_id, UUID(data["request_id"]))
    require_master(request, user.id)
    service = await db_session.get(Service, request.service_id)
    if service is None:
        raise ConversationAccessError("Service unavailable")
    try:
        amount = parse_money(message.text or "", service.currency)
    except ValueError:
        await message.answer(f"Введите стоимость числом в {service.currency}, от 0 до 20 000 000.")
        return
    await state.update_data(amount_minor=amount, currency=service.currency)
    await state.set_state(RequestStates.comment)
    await message.answer(
        "Комментарий к предложению? Максимум 2000 символов.",
        reply_markup=keyboard(("Пропустить", "rq:skip"), ("Отмена", cb("view", request.id))),
    )


async def preview_price(target, state, comment=None):
    data = await state.get_data()
    await state.update_data(comment=comment)
    await state.set_state(RequestStates.proposal_confirm)
    text = (
        f"Стоимость: {format_money(data['amount_minor'], data['currency'])}\n"
        f"Комментарий:\n{escape(comment or 'Без комментария')}"
    )
    buttons = keyboard(
        ("Отправить клиенту", "rq:propose"),
        ("Изменить", cb("price", UUID(data["request_id"]))),
        ("Отмена", cb("view", UUID(data["request_id"]))),
    )
    if isinstance(target, CallbackQuery):
        await edit_or_answer(target, text, reply_markup=buttons)
    else:
        await target.answer(text, reply_markup=buttons)


@router.message(RequestStates.comment)
@guarded
async def comment_input(message: Message, state: FSMContext):
    if not message.text or message.text.startswith("/"):
        await message.answer("Отправьте текст комментария или нажмите «Пропустить».")
        return
    if not message.text.strip() or len(message.text) > MAX_COMMENT_LENGTH:
        await message.answer(f"Комментарий должен содержать 1–{MAX_COMMENT_LENGTH} символов.")
        return
    await preview_price(message, state, message.text)


@router.callback_query(F.data.startswith("rq:"))
@guarded
async def request_callback(
    callback: CallbackQuery, state: FSMContext, db_session: AsyncSession, business_id: UUID
):
    parts = (callback.data or "").split(":")
    action = parts[1]
    user = await _actor(callback, db_session)
    current = await state.get_state()
    if action == "abort":
        await state.clear()
        await edit_or_answer(callback, "Ввод отменён.", reply_markup=main_menu_keyboard())
        if isinstance(callback.message, Message):
            await callback.message.answer("Главное меню", reply_markup=ReplyKeyboardRemove())
    elif action in {"attach", "submit"}:
        if current != RequestStates.collecting.state:
            raise ValueError("Expired request input")
        if action == "submit":
            await submit_request(callback, state, db_session, business_id)
        else:
            await edit_or_answer(
                callback,
                "Отправьте фотографии или документы. "
                "Все фото альбома сохранятся. Затем нажмите «Отправить заявку».",
                reply_markup=input_keyboard(),
            )
    elif action in {"skip", "propose"}:
        if action == "skip":
            if current != RequestStates.comment.state:
                raise ValueError("Expired price input")
            await preview_price(callback, state)
        else:
            if current != RequestStates.proposal_confirm.state:
                raise ValueError("Expired confirmation")
            data = await state.get_data()
            request_id = UUID(data["request_id"])
            await PRICES.propose(
                db_session,
                business_id=business_id,
                request_id=request_id,
                actor_user_id=user.id,
                amount_minor=data["amount_minor"],
                currency=data["currency"],
                comment=data.get("comment"),
            )
            await state.clear()
            await persist_before_response(db_session, state)
            await show_card(callback, state, db_session, business_id, request_id)
    elif action in {"mine", "inbox", "list"}:
        master = action == "inbox" or (action == "list" and parts[2] == "m")
        if (
            master
            and await get_master_for_user(db_session, business_id=business_id, user_id=user.id)
            is None
        ):
            raise ConversationAccessError("Master required")
        await state.clear()
        if action != "list":
            sections = (
                [
                    ("Новые", "new"),
                    ("Ждут моего ответа", "reply"),
                    ("Ждут клиента", "client"),
                    ("Согласованы", "agreed"),
                    ("Закрытые", "closed"),
                ]
                if master
                else [("Активные", "active"), ("Завершённые", "done")]
            )
            unread = await CHAT.total_unread(
                db_session, business_id=business_id, actor_user_id=user.id
            )
            await edit_or_answer(
                callback,
                ("Заявки и диалоги" if master else "Мои заявки")
                + (f" 🔴 {unread}" if unread else ""),
                reply_markup=keyboard(
                    *(
                        (label, f"rq:list:{'m' if master else 'c'}:{code}:0")
                        for label, code in sections
                    ),
                    ("Назад", "master:menu" if master else "menu:home"),
                ),
            )
        else:
            section, offset = parts[3], int(parts[4])
            allowed = (
                {"new", "reply", "client", "agreed", "closed"} if master else {"active", "done"}
            )
            if section not in allowed:
                raise ValueError("Invalid section")
            requests = await REQUESTS.list_requests(
                db_session,
                business_id=business_id,
                actor_user_id=user.id,
                statuses=SECTIONS[section],
                limit=PAGE_SIZE,
                offset=offset,
                unopened_by_actor=section == "new",
            )
            buttons = []
            business = await db_session.get(Business, business_id)
            unread_counts = await CHAT.unread_by_request(
                db_session,
                business_id=business_id,
                actor_user_id=user.id,
                request_ids=[request.id for request in requests],
            )
            for request in requests:
                unread = unread_counts.get(request.id, 0)
                created = request.created_at.astimezone(ZoneInfo(business.timezone))
                label = f"{request.client_name_snapshot} · " if master else ""
                label += (
                    f"{request.service_name_snapshot} · {STATUS_LABELS[request.status]}"
                    f" · {created:%d.%m %H:%M}"
                )
                if unread:
                    label += f" 🔴 {unread}"
                buttons.append((label, cb("view", request.id)))
            prefix = f"rq:list:{'m' if master else 'c'}:{section}"
            if len(requests) == PAGE_SIZE:
                buttons.append(("Следующие", f"{prefix}:{offset + PAGE_SIZE}"))
            if offset:
                buttons.append(("Предыдущие", f"{prefix}:{max(0, offset - PAGE_SIZE)}"))
            buttons.extend(
                [("Назад", "rq:inbox" if master else "rq:mine"), ("Главное меню", "menu:home")]
            )
            await edit_or_answer(
                callback,
                "Выберите заявку:" if requests else "Заявок пока нет.",
                reply_markup=keyboard(*buttons),
            )
    else:
        identifier = UUID(parts[2])
        if action in {"accept", "reject"}:
            proposal = await PRICES.decide_by_id(
                db_session,
                business_id=business_id,
                actor_user_id=user.id,
                proposal_id=identifier,
                accept=action == "accept",
            )
            await state.clear()
            await persist_before_response(db_session, state)
            await show_card(callback, state, db_session, business_id, proposal.booking_request_id)
            await callback.answer(
                "✅ Стоимость согласована." if action == "accept" else "Предложение отклонено."
            )
            return
        if action == "appointment":
            request = await REQUESTS.for_appointment(
                db_session,
                business_id=business_id,
                appointment_id=identifier,
                actor_user_id=user.id,
            )
            identifier = request.id
            action = "chat"
        user, request = await _context(callback, db_session, business_id, identifier)
        if action == "view":
            await show_card(callback, state, db_session, business_id, identifier)
        elif action == "chat":
            await show_conversation(
                callback,
                state,
                db_session,
                business_id,
                identifier,
                int(parts[3]) if len(parts) > 3 else None,
            )
        elif action == "reply":
            conversation = await CHAT.get(
                db_session, business_id=business_id, request_id=identifier, actor_user_id=user.id
            )
            if conversation.status != "open":
                raise ConversationClosedError("Closed")
            await state.set_data({"request_id": str(identifier)})
            await state.set_state(RequestStates.reply)
            await edit_or_answer(
                callback,
                "Отправьте текст, фотографию или документ в этот диалог. "
                "После одиночного сообщения ввод завершится. После альбома нажмите «Готово». "
                "/cancel — отменить ввод.",
                reply_markup=keyboard(
                    ("Готово / Назад", cb("view", identifier)),
                    ("Отмена", cb("view", identifier)),
                    ("Главное меню", "menu:home"),
                ),
            )
        elif action == "price":
            require_master(request, user.id)
            await state.set_data({"request_id": str(identifier)})
            await state.set_state(RequestStates.price)
            service = await db_session.get(Service, request.service_id)
            await edit_or_answer(
                callback,
                f"Введите стоимость в {service.currency}:",
                reply_markup=keyboard(("Отмена", cb("view", identifier))),
            )
        elif action == "offer":
            proposals = await PRICES.list_proposals(
                db_session,
                business_id=business_id,
                request_id=identifier,
                actor_user_id=user.id,
                limit=1,
            )
            if not proposals:
                raise ProposalNotPendingError("Missing proposal")
            proposal = proposals[0]
            buttons = []
            if request.client_user_id == user.id and proposal.status == "pending":
                buttons.extend(
                    [
                        ("Принять", cb("accept", proposal.id)),
                        ("Отклонить", cb("reject", proposal.id)),
                    ]
                )
            buttons.extend(
                [("Обсудить", cb("chat", identifier)), ("Назад к заявке", cb("view", identifier))]
            )
            await state.clear()
            await edit_or_answer(
                callback,
                "💰 Мастер предложил стоимость\n\n"
                f"{format_money(proposal.amount_minor, proposal.currency)}"
                f"\n\n{escape(proposal.comment or '')}",
                reply_markup=keyboard(*buttons),
            )
        elif action in {"close", "cancel", "yesclose", "yescancel"}:
            if "close" in action:
                require_master(request, user.id)
            if action.startswith("yes"):
                operation = REQUESTS.close if action == "yesclose" else REQUESTS.cancel
                await operation(
                    db_session,
                    business_id=business_id,
                    request_id=identifier,
                    actor_user_id=user.id,
                )
                await state.clear()
                await persist_before_response(db_session, state)
                await show_card(callback, state, db_session, business_id, identifier)
            else:
                await state.clear()
                await edit_or_answer(
                    callback,
                    "Закрыть заявку/диалог?" if action == "close" else "Отменить заявку?",
                    reply_markup=keyboard(
                        ("Да", cb("yes" + action, identifier)), ("Нет", cb("view", identifier))
                    ),
                )
        elif action == "book":
            # list_slots verifies client ownership and latest accepted terms in the service.
            from datetime import UTC, datetime

            from booking_bot.bot.handlers.booking import _settings, _show_dates

            business = await db_session.get(Business, business_id)
            today = datetime.now(UTC).astimezone(ZoneInfo(business.timezone)).date()
            await REQUESTS.list_slots(
                db_session,
                settings=_settings(),
                business_id=business_id,
                request_id=identifier,
                actor_user_id=user.id,
                local_date=today,
            )
            await state.set_data(
                {
                    "request_id": str(identifier),
                    "master_id": str(request.master_id),
                    "service_id": str(request.service_id),
                    "client_id": str(user.id),
                }
            )
            await _show_dates(callback, state, db_session, business_id)
            return
        elif action == "appt":
            # Lookup is authorized against the request before navigating existing screens.
            from sqlalchemy import select

            from booking_bot.db.models import Appointment

            appointment_id = await db_session.scalar(
                select(Appointment.id).where(
                    Appointment.booking_request_id == identifier,
                    Appointment.business_id == business_id,
                )
            )
            if appointment_id is None:
                raise InvalidRequestTransitionError("Appointment not created")
            if request.client_user_id == user.id:
                from booking_bot.bot.handlers.booking import view_client_appointment

                await view_client_appointment(
                    callback.model_copy(update={"data": f"appt:v:{appointment_id}"}),
                    state,
                    db_session,
                    business_id,
                )
            else:
                from booking_bot.bot.handlers.master import master_appointment

                await master_appointment(
                    callback.model_copy(update={"data": f"master:appointment:{appointment_id}"}),
                    db_session,
                    business_id,
                )
            return
        else:
            raise ValueError("Unknown action")
    await callback.answer()

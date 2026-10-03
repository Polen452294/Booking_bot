from datetime import UTC, date, datetime, timedelta
from html import escape
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from booking_bot.bot.conversation_ui import edit_or_answer
from booking_bot.bot.keyboards import (
    client_appointment_actions_keyboard,
    client_appointments_keyboard,
    client_booking_sections_keyboard,
    client_cancel_confirmation_keyboard,
    confirmation_keyboard,
    dates_keyboard,
    main_menu_keyboard,
    phone_keyboard,
    reschedule_confirmation_keyboard,
    services_keyboard,
    slots_keyboard,
)
from booking_bot.bot.states import BookingStates
from booking_bot.config import Settings, get_settings
from booking_bot.db.models import (
    Appointment,
    Business,
    CalendarEntry,
    Master,
    MasterService,
    Service,
    TelegramUser,
)
from booking_bot.domain.conversations import ConversationError
from booking_bot.domain.enums import PricingMode
from booking_bot.services.availability import AvailabilityService, BookingConfigurationError
from booking_bot.services.booking_requests import BookingRequestService
from booking_bot.services.bookings import (
    AppointmentChangeNotAllowedError,
    AppointmentNotFoundError,
    AppointmentSummary,
    BookingService,
    ClientPhoneRequiredError,
    HoldExpiredError,
    HoldNotFoundError,
    HoldSummary,
    SlotUnavailableError,
)
from booking_bot.services.users import (
    get_or_create_telegram_user,
    normalize_phone,
    set_user_phone,
)
from booking_bot.specialist_config import get_specialist_template

router = Router(name="booking")


def _settings() -> Settings:
    return get_settings()


def _template():
    return get_specialist_template()


def _message_from_callback(callback: CallbackQuery) -> Message | None:
    return callback.message if isinstance(callback.message, Message) else None


async def _edit_or_answer(callback: CallbackQuery, text: str, **kwargs) -> None:
    await edit_or_answer(callback, text, **kwargs)


def _format_hold(summary: HoldSummary) -> str:
    location = f"\nАдрес: <b>{escape(summary.location_name)}</b>" if summary.location_name else ""
    return (
        "Проверьте запись:\n\n"
        f"Услуга: <b>{escape(summary.service_name)}</b>\n"
        f"Дата: <b>{summary.local_start:%d.%m.%Y}</b>\n"
        f"Время: <b>{summary.local_start:%H:%M}-{summary.local_end:%H:%M}</b>"
        f"{location}\n\n"
        "Время временно удерживается за вами."
    )


def _format_reschedule_hold(summary: HoldSummary) -> str:
    location = f"\nАдрес: <b>{escape(summary.location_name)}</b>" if summary.location_name else ""
    return (
        "Проверьте новое время:\n\n"
        f"Услуга: <b>{escape(summary.service_name)}</b>\n"
        f"Дата: <b>{summary.local_start:%d.%m.%Y}</b>\n"
        f"Время: <b>{summary.local_start:%H:%M}-{summary.local_end:%H:%M}</b>"
        f"{location}\n\n"
        "Старая запись сохранится, пока вы не подтвердите перенос."
    )


def _format_appointment(summary: AppointmentSummary) -> str:
    status_labels = {
        "pending_approval": "Ожидает подтверждения",
        "pending_payment": "Ожидает оплаты",
        "confirmed": "Подтверждена",
        "cancelled_by_client": "Отменена вами",
        "cancelled_by_master": "Отменена специалистом",
        "completed": "Выполнена",
        "no_show": "Не состоялась",
    }
    location = f"\nАдрес: <b>{escape(summary.location_name)}</b>" if summary.location_name else ""
    active = summary.status in {"pending_approval", "pending_payment", "confirmed"}
    change_note = ""
    if active:
        change_note = (
            f"\n\nПеренос и отмена доступны до <b>{summary.change_deadline:%d.%m.%Y %H:%M}</b>."
            if summary.can_change
            else "\n\nСрок самостоятельного переноса и отмены уже истёк."
        )
    return (
        f"<b>{escape(summary.service_name)}</b>\n\n"
        f"Статус: <b>{status_labels.get(summary.status, escape(summary.status))}</b>\n"
        f"Специалист: <b>{escape(summary.master_name)}</b>\n"
        f"Дата: <b>{summary.local_start:%d.%m.%Y}</b>\n"
        f"Время: <b>{summary.local_start:%H:%M}-{summary.local_end:%H:%M}</b>"
        f"{location}{change_note}"
    )


def _ics_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def _build_appointment_ics(summary: AppointmentSummary) -> bytes:
    location = ", ".join(
        value for value in (summary.location_name, summary.location_address) if value
    )
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Telegram Specialist Booking Bot//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{summary.appointment_id}@telegram-booking-bot",
        f"DTSTAMP:{datetime.now(UTC):%Y%m%dT%H%M%SZ}",
        f"DTSTART:{summary.local_start.astimezone(UTC):%Y%m%dT%H%M%SZ}",
        f"DTEND:{summary.local_end.astimezone(UTC):%Y%m%dT%H%M%SZ}",
        f"SUMMARY:{_ics_escape(summary.service_name)}",
        f"DESCRIPTION:{_ics_escape('Запись к специалисту ' + summary.master_name)}",
    ]
    if location:
        lines.append(f"LOCATION:{_ics_escape(location)}")
    lines.extend(["END:VEVENT", "END:VCALENDAR", ""])
    return "\r\n".join(lines).encode("utf-8")


async def _show_confirmation(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    try:
        summary = await BookingService(_settings()).get_hold_summary(
            session,
            hold_id=UUID(data["hold_id"]),
            client_id=UUID(data["client_id"]),
        )
    except (KeyError, ValueError, HoldNotFoundError):
        await state.clear()
        await message.answer(
            "Не удалось найти удерживаемое время. Начните запись заново.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await state.set_state(BookingStates.confirming)
    await message.answer(_format_hold(summary), reply_markup=confirmation_keyboard())


async def _show_dates(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    business_id: UUID,
) -> None:
    data = await state.get_data()
    try:
        master_id = UUID(data["master_id"])
        service_id = UUID(data["service_id"])
    except (KeyError, ValueError):
        await callback.answer("Начните выбор даты заново", show_alert=True)
        return
    master = await session.get(Master, master_id)
    business = await session.get(Business, business_id)
    if master is None or business is None:
        await callback.answer("Запись временно недоступна", show_alert=True)
        return
    timezone = ZoneInfo(master.timezone or business.timezone)
    today = datetime.now(UTC).astimezone(timezone).date()
    candidate_dates = [
        today + timedelta(days=offset) for offset in range(_settings().booking_dates_shown)
    ]
    dates = await AvailabilityService(_settings()).list_available_dates(
        session,
        business_id=business_id,
        master_id=master_id,
        service_id=service_id,
        local_dates=candidate_dates,
    )
    await state.set_state(BookingStates.selecting_date)
    await _edit_or_answer(
        callback,
        (
            "Выберите дату:"
            if dates
            else "В ближайшие дни свободных окон нет. Попробуйте проверить расписание позже."
        ),
        reply_markup=dates_keyboard(
            dates,
            back_callback=(
                f"rq:view:{UUID(data['request_id']).hex}"
                if data.get("request_id")
                else "booking:start"
            ),
        ),
    )
    await callback.answer()


async def _show_reschedule_dates(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    business_id: UUID,
) -> None:
    data = await state.get_data()
    try:
        master_id = UUID(data["master_id"])
        service_id = UUID(data["service_id"])
        appointment_id = UUID(data["appointment_id"])
    except (KeyError, ValueError):
        await state.clear()
        await callback.answer("Начните перенос заново", show_alert=True)
        return
    master = await session.get(Master, master_id)
    business = await session.get(Business, business_id)
    if master is None or business is None:
        await callback.answer("Перенос временно недоступен", show_alert=True)
        return
    timezone = ZoneInfo(master.timezone or business.timezone)
    today = datetime.now(UTC).astimezone(timezone).date()
    candidate_dates = [
        today + timedelta(days=offset) for offset in range(_settings().booking_dates_shown)
    ]
    dates = await AvailabilityService(_settings()).list_available_dates(
        session,
        business_id=business_id,
        master_id=master_id,
        service_id=service_id,
        local_dates=candidate_dates,
    )
    await state.set_state(BookingStates.rescheduling_date)
    await _edit_or_answer(
        callback,
        ("Выберите новую дату:" if dates else "В ближайшие дни нет свободных окон для переноса."),
        reply_markup=dates_keyboard(
            dates,
            callback_prefix="rdate",
            back_callback=f"appt:v:{appointment_id}",
            back_text="Назад к записи",
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:home")
async def home_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    data = await state.get_data()
    if "hold_id" in data and "client_id" in data:
        await BookingService(_settings()).release_hold(
            db_session,
            hold_id=UUID(data["hold_id"]),
            client_id=UUID(data["client_id"]),
        )
    await state.clear()
    from booking_bot.services.conversations import ConversationService
    from booking_bot.services.master_access import get_master_for_user

    user = await get_or_create_telegram_user(db_session, callback.from_user)
    actor_master = await get_master_for_user(db_session, business_id=business_id, user_id=user.id)
    await _edit_or_answer(
        callback,
        "Главное меню:",
        reply_markup=main_menu_keyboard(
            master_access=actor_master is not None,
            unread_count=await ConversationService().total_unread(
                db_session, business_id=business_id, actor_user_id=user.id
            ),
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery) -> None:
    await _edit_or_answer(
        callback,
        _template().text(
            "help",
            "Выберите услугу, дату и свободное время. "
            "До подтверждения выбранный слот удерживается 10 минут.",
        ),
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "booking:start")
async def booking_start(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    services = list(
        (
            await db_session.scalars(
                select(Service)
                .where(
                    Service.business_id == business_id,
                    Service.is_active.is_(True),
                )
                .order_by(Service.name)
            )
        ).all()
    )
    if not services:
        await callback.answer("Услуги пока не настроены", show_alert=True)
        return
    await state.clear()
    await state.set_state(BookingStates.selecting_service)
    await _edit_or_answer(
        callback,
        _template().text("booking_intro", "Выберите услугу:"),
        reply_markup=services_keyboard(services),
    )
    await callback.answer()


@router.callback_query(BookingStates.selecting_service, F.data.startswith("service:"))
async def select_service(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
    specialist_master_id: UUID,
) -> None:
    try:
        service_id = UUID(callback.data.split(":", 1)[1])
    except (AttributeError, ValueError):
        await callback.answer("Некорректная услуга", show_alert=True)
        return

    available = await db_session.scalar(
        select(MasterService.id)
        .join(Service, Service.id == MasterService.service_id)
        .join(Master, Master.id == MasterService.master_id)
        .where(
            Service.id == service_id,
            Service.business_id == business_id,
            Service.is_active.is_(True),
            MasterService.business_id == business_id,
            MasterService.master_id == specialist_master_id,
            MasterService.is_active.is_(True),
            Master.is_active.is_(True),
        )
    )
    if available is None:
        await callback.answer("Услуга временно недоступна", show_alert=True)
        return
    await state.update_data(
        service_id=str(service_id),
        master_id=str(specialist_master_id),
    )
    service = await db_session.get(Service, service_id)
    if service.pricing_mode == PricingMode.NEGOTIABLE.value:
        from booking_bot.bot.handlers.requests import begin_request

        await begin_request(callback, state)
        await callback.answer()
        return
    if service.pricing_mode == PricingMode.FROM.value:
        from booking_bot.bot.conversation_ui import keyboard
        from booking_bot.domain.money import format_money

        await state.set_state(BookingStates.choosing_pricing_flow)
        price = (
            format_money(service.price_minor, service.currency)
            if service.price_minor is not None
            else "уточняется"
        )
        await _edit_or_answer(
            callback,
            f"Цена: от {price}",
            reply_markup=keyboard(
                ("Записаться", "rq:from:book"),
                ("Сначала обсудить", "rq:from:discuss"),
                ("Назад", "booking:start"),
                ("Главное меню", "menu:home"),
            ),
        )
        await callback.answer()
        return
    await _show_dates(callback, state, db_session, business_id)


@router.callback_query(F.data == "booking:back:dates")
async def back_to_dates(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    await _show_dates(callback, state, db_session, business_id)


@router.callback_query(BookingStates.selecting_date, F.data.startswith("date:"))
async def select_date(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    try:
        local_date = date.fromisoformat(callback.data.split(":", 1)[1])
        data = await state.get_data()
        service_id = UUID(data["service_id"])
        master_id = UUID(data["master_id"])
    except (AttributeError, KeyError, ValueError):
        await callback.answer("Начните выбор заново", show_alert=True)
        return

    try:
        if data.get("request_id"):
            user = await get_or_create_telegram_user(db_session, callback.from_user)
            slots = await BookingRequestService().list_slots(
                db_session,
                settings=_settings(),
                business_id=business_id,
                request_id=UUID(data["request_id"]),
                actor_user_id=user.id,
                local_date=local_date,
            )
        else:
            slots = await AvailabilityService(_settings()).list_slots(
                db_session,
                business_id=business_id,
                master_id=master_id,
                service_id=service_id,
                local_date=local_date,
            )
    except ConversationError:
        await callback.answer("Условия заявки изменились. Откройте заявку заново.", show_alert=True)
        return
    except BookingConfigurationError:
        await callback.answer("Расписание настроено некорректно", show_alert=True)
        return

    master = await db_session.get(Master, master_id)
    business = await db_session.get(Business, business_id)
    if master is None or business is None:
        await callback.answer("Запись временно недоступна", show_alert=True)
        return
    timezone = ZoneInfo(master.timezone or business.timezone)
    await state.update_data(local_date=local_date.isoformat())
    if not slots:
        await state.set_state(BookingStates.selecting_date)
        candidate_dates = [
            datetime.now(UTC).astimezone(timezone).date() + timedelta(days=offset)
            for offset in range(_settings().booking_dates_shown)
        ]
        available_dates = await AvailabilityService(_settings()).list_available_dates(
            db_session,
            business_id=business_id,
            master_id=master_id,
            service_id=service_id,
            local_dates=candidate_dates,
        )
        await _edit_or_answer(
            callback,
            "На эту дату свободных окон нет. Выберите другую дату:",
            reply_markup=dates_keyboard(available_dates),
        )
    else:
        await state.set_state(BookingStates.selecting_slot)
        await _edit_or_answer(
            callback,
            f"Свободное время на {local_date:%d.%m.%Y}:",
            reply_markup=slots_keyboard(slots, timezone),
        )
    await callback.answer()


@router.callback_query(BookingStates.selecting_slot, F.data.startswith("slot:"))
async def select_slot(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    if callback.from_user is None:
        return
    try:
        service_start = datetime.fromtimestamp(int(callback.data.split(":", 1)[1]), tz=UTC)
        data = await state.get_data()
        service_id = UUID(data["service_id"])
        master_id = UUID(data["master_id"])
        local_date = date.fromisoformat(data["local_date"])
    except (AttributeError, KeyError, TypeError, ValueError):
        await callback.answer("Начните выбор заново", show_alert=True)
        return

    user = await get_or_create_telegram_user(db_session, callback.from_user)
    try:
        if data.get("request_id"):
            hold = await BookingRequestService().create_hold(
                db_session,
                settings=_settings(),
                business_id=business_id,
                request_id=UUID(data["request_id"]),
                actor_user_id=user.id,
                service_start=service_start,
                local_date=local_date,
            )
        else:
            hold = await BookingService(_settings()).create_hold(
                db_session,
                business_id=business_id,
                master_id=master_id,
                service_id=service_id,
                client_id=user.id,
                service_start=service_start,
                local_date=local_date,
            )
    except ConversationError:
        await callback.answer("Условия заявки изменились. Откройте заявку заново.", show_alert=True)
        return
    except SlotUnavailableError:
        await callback.answer(
            "Это время уже занято. Выберите другое.",
            show_alert=True,
        )
        return

    await state.update_data(hold_id=str(hold.id), client_id=str(user.id))
    message = _message_from_callback(callback)
    if user.phone:
        await state.set_state(BookingStates.confirming)
        if message is not None:
            summary = await BookingService(_settings()).get_hold_summary(
                db_session, hold_id=hold.id, client_id=user.id
            )
            await _edit_or_answer(
                callback, _format_hold(summary), reply_markup=confirmation_keyboard()
            )
    else:
        await state.set_state(BookingStates.waiting_phone)
        if message is not None:
            await message.edit_text("Время удерживается 10 минут.")
            await message.answer(
                "Отправьте номер телефона для связи:",
                reply_markup=phone_keyboard(),
            )
    await callback.answer()


@router.message(BookingStates.waiting_phone, F.contact)
async def receive_contact(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    if message.from_user is None or message.contact is None:
        return
    if message.contact.user_id not in (None, message.from_user.id):
        await message.answer("Отправьте, пожалуйста, собственный номер телефона.")
        return
    data = await state.get_data()
    user = await db_session.get(TelegramUser, UUID(data["client_id"]))
    if user is None:
        await state.clear()
        return
    merged_appointment_ids = await set_user_phone(
        db_session,
        user,
        message.contact.phone_number,
    )
    await BookingService(_settings()).schedule_client_reminders_for_appointments(
        db_session,
        appointment_ids=merged_appointment_ids,
    )
    from booking_bot.bot.middlewares import persist_before_response

    await persist_before_response(db_session, state)
    await message.answer("Телефон сохранен.", reply_markup=ReplyKeyboardRemove())
    await _show_confirmation(message, state, db_session)


@router.message(BookingStates.waiting_phone, F.text)
async def receive_phone_text(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    raw_phone = message.text or ""
    normalized = normalize_phone(raw_phone)
    if normalized is None:
        await message.answer("Введите телефон из 10-15 цифр или используйте кнопку ниже.")
        return
    data = await state.get_data()
    user = await db_session.get(TelegramUser, UUID(data["client_id"]))
    if user is None:
        await state.clear()
        return
    merged_appointment_ids = await set_user_phone(db_session, user, normalized)
    await BookingService(_settings()).schedule_client_reminders_for_appointments(
        db_session,
        appointment_ids=merged_appointment_ids,
    )
    await message.answer("Телефон сохранен.", reply_markup=ReplyKeyboardRemove())
    await _show_confirmation(message, state, db_session)


@router.callback_query(BookingStates.confirming, F.data == "booking:confirm")
async def confirm_booking(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    data = await state.get_data()
    try:
        user = await get_or_create_telegram_user(db_session, callback.from_user)
        if data.get("request_id"):
            summary = await BookingRequestService().book(
                db_session,
                settings=_settings(),
                business_id=business_id,
                request_id=UUID(data["request_id"]),
                actor_user_id=user.id,
                hold_id=UUID(data["hold_id"]),
            )
        else:
            summary = await BookingService(_settings()).confirm_hold(
                db_session,
                hold_id=UUID(data["hold_id"]),
                client_id=user.id,
            )
    except ConversationError:
        await callback.answer(
            "Заявка уже записана или условия изменились. Откройте её заново.", show_alert=True
        )
        return
    except SlotUnavailableError:
        await callback.answer("Этот слот уже занят. Выберите другое время.", show_alert=True)
        await _show_dates(callback, state, db_session, business_id)
        return
    except ClientPhoneRequiredError:
        await state.set_state(BookingStates.waiting_phone)
        await callback.answer("Нужен номер телефона", show_alert=True)
        message = _message_from_callback(callback)
        if message is not None:
            await message.answer(
                "Отправьте номер телефона для связи:",
                reply_markup=phone_keyboard(),
            )
        return
    except (
        KeyError,
        ValueError,
        HoldNotFoundError,
        HoldExpiredError,
    ):
        await state.clear()
        await callback.answer("Время удержания истекло", show_alert=True)
        await _edit_or_answer(
            callback,
            "Выбранное время больше не удерживается.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await state.clear()
    from booking_bot.bot.middlewares import persist_before_response

    await persist_before_response(db_session, state)
    status_text = (
        "Ожидает подтверждения" if summary.status == "pending_approval" else "Подтверждена"
    )
    location = f"\nАдрес: <b>{escape(summary.location_name)}</b>" if summary.location_name else ""
    await _edit_or_answer(
        callback,
        _template().text(
            "booking_success",
            "Запись создана!",
        )
        + "\n\n"
        f"Статус: <b>{status_text}</b>\n"
        f"Услуга: <b>{escape(summary.service_name)}</b>\n"
        f"Дата и время: <b>{summary.local_start:%d.%m.%Y %H:%M}</b>"
        f"{location}",
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer("Запись подтверждена")


@router.callback_query(F.data == "booking:confirm")
async def stale_booking_confirmation(callback: CallbackQuery) -> None:
    await callback.answer(
        "Запись уже создана или подтверждение устарело. Откройте «Мои записи» / «Мои заявки».",
        show_alert=True,
    )


@router.callback_query(F.data == "booking:abort")
async def abort_booking(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
) -> None:
    data = await state.get_data()
    if "hold_id" in data and "client_id" in data:
        await BookingService(_settings()).release_hold(
            db_session,
            hold_id=UUID(data["hold_id"]),
            client_id=UUID(data["client_id"]),
        )
    await state.clear()
    await _edit_or_answer(
        callback,
        "Запись отменена до подтверждения.",
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "booking:mine")
async def my_bookings(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    await get_or_create_telegram_user(db_session, callback.from_user)
    await state.clear()
    await _edit_or_answer(
        callback,
        "<b>Мои записи</b>\n\nВыберите раздел:",
        reply_markup=client_booking_sections_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("booking:list:"))
async def client_booking_list(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    if callback.from_user is None:
        return
    scope = (callback.data or "").rsplit(":", 1)[-1]
    user = await get_or_create_telegram_user(db_session, callback.from_user)
    service = BookingService(_settings())
    if scope == "upcoming":
        appointments = await service.list_upcoming(
            db_session,
            business_id=business_id,
            client_id=user.id,
        )
        title = "Предстоящие записи"
    elif scope == "past":
        appointments = await service.list_past(
            db_session,
            business_id=business_id,
            client_id=user.id,
        )
        title = "Прошлые записи"
    elif scope == "cancelled":
        appointments = await service.list_cancelled(
            db_session,
            business_id=business_id,
            client_id=user.id,
        )
        title = "Отменённые записи"
    else:
        await callback.answer("Неизвестный раздел", show_alert=True)
        return
    await state.clear()
    text = (
        f"<b>{title}</b>\n\nВыберите запись, чтобы посмотреть детали."
        if appointments
        else f"<b>{title}</b>\n\nЗдесь пока нет записей."
    )
    await _edit_or_answer(
        callback,
        text,
        reply_markup=(
            client_appointments_keyboard(appointments)
            if appointments
            else client_booking_sections_keyboard()
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("appt:v:"))
async def view_client_appointment(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    if callback.from_user is None:
        return
    try:
        appointment_id = UUID(callback.data.split(":", 2)[2])
    except (AttributeError, ValueError):
        await callback.answer("Некорректная запись", show_alert=True)
        return
    user = await get_or_create_telegram_user(db_session, callback.from_user)
    try:
        summary = await BookingService(_settings()).get_appointment(
            db_session,
            business_id=business_id,
            client_id=user.id,
            appointment_id=appointment_id,
        )
    except AppointmentNotFoundError:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    await state.clear()
    await _edit_or_answer(
        callback,
        _format_appointment(summary),
        reply_markup=client_appointment_actions_keyboard(summary),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("appt:repeat:"))
async def repeat_client_appointment(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
    specialist_master_id: UUID,
) -> None:
    if callback.from_user is None:
        return
    try:
        appointment_id = UUID((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Некорректная запись", show_alert=True)
        return
    user = await get_or_create_telegram_user(db_session, callback.from_user)
    try:
        summary = await BookingService(_settings()).get_appointment(
            db_session,
            business_id=business_id,
            client_id=user.id,
            appointment_id=appointment_id,
        )
    except AppointmentNotFoundError:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    if summary.service_id is None:
        await callback.answer("Эта услуга больше недоступна", show_alert=True)
        return
    available = await db_session.scalar(
        select(MasterService.id)
        .join(Service, Service.id == MasterService.service_id)
        .where(
            MasterService.business_id == business_id,
            MasterService.master_id == specialist_master_id,
            MasterService.service_id == summary.service_id,
            MasterService.is_active.is_(True),
            Service.is_active.is_(True),
        )
    )
    if available is None:
        await callback.answer("Эта услуга сейчас недоступна", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        service_id=str(summary.service_id),
        master_id=str(specialist_master_id),
    )
    await state.set_state(BookingStates.selecting_service)
    await select_service(
        callback.model_copy(update={"data": f"service:{summary.service_id}"}),
        state,
        db_session,
        business_id,
        specialist_master_id,
    )


@router.callback_query(F.data.startswith("appt:contact:"))
async def contact_specialist(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    if callback.from_user is None:
        return
    try:
        appointment_id = UUID((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Некорректная запись", show_alert=True)
        return
    client = await get_or_create_telegram_user(db_session, callback.from_user)
    try:
        summary = await BookingService(_settings()).get_appointment(
            db_session,
            business_id=business_id,
            client_id=client.id,
            appointment_id=appointment_id,
        )
    except AppointmentNotFoundError:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    if summary.booking_request_id:
        from booking_bot.bot.handlers.requests import show_conversation

        await show_conversation(
            callback, state, db_session, business_id, summary.booking_request_id
        )
        await callback.answer()
        return
    specialist_user = await db_session.scalar(
        select(TelegramUser)
        .join(Master, Master.user_id == TelegramUser.id)
        .join(CalendarEntry, CalendarEntry.master_id == Master.id)
        .join(Appointment, Appointment.calendar_entry_id == CalendarEntry.id)
        .where(
            Appointment.id == appointment_id,
            Appointment.business_id == business_id,
            Appointment.client_id == client.id,
        )
    )
    if (
        specialist_user is None
        or specialist_user.telegram_user_id is None
        or not isinstance(callback.message, Message)
    ):
        await callback.answer("Контакт специалиста пока не указан", show_alert=True)
        return
    if specialist_user.username:
        link = f"https://t.me/{specialist_user.username}"
    else:
        link = f"tg://user?id={specialist_user.telegram_user_id}"
    await callback.message.answer(f'Связаться со специалистом: <a href="{link}">открыть чат</a>')
    await callback.answer()


@router.callback_query(F.data.startswith("appt:ics:"))
async def download_appointment_calendar(
    callback: CallbackQuery,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        return
    try:
        appointment_id = UUID((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Некорректная запись", show_alert=True)
        return
    client = await get_or_create_telegram_user(db_session, callback.from_user)
    try:
        summary = await BookingService(_settings()).get_appointment(
            db_session,
            business_id=business_id,
            client_id=client.id,
            appointment_id=appointment_id,
        )
    except AppointmentNotFoundError:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    document = BufferedInputFile(
        _build_appointment_ics(summary),
        filename=f"appointment-{summary.local_start:%Y-%m-%d}.ics",
    )
    await callback.message.answer_document(
        document,
        caption="Событие для Google Calendar, Apple Calendar и Outlook.",
    )
    await callback.answer("Файл календаря готов")


@router.callback_query(F.data.startswith("appt:c:"))
async def request_client_cancellation(
    callback: CallbackQuery,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    if callback.from_user is None:
        return
    try:
        appointment_id = UUID(callback.data.split(":", 2)[2])
    except (AttributeError, ValueError):
        await callback.answer("Некорректная запись", show_alert=True)
        return
    user = await get_or_create_telegram_user(db_session, callback.from_user)
    try:
        summary = await BookingService(_settings()).get_appointment(
            db_session,
            business_id=business_id,
            client_id=user.id,
            appointment_id=appointment_id,
        )
    except AppointmentNotFoundError:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    if not summary.can_change:
        await callback.answer(
            f"Отмена доступна не позднее чем за "
            f"{_settings().cancellation_cutoff_hours} ч. до записи",
            show_alert=True,
        )
        return
    await _edit_or_answer(
        callback,
        "Точно отменить эту запись?\n\n" + _format_appointment(summary),
        reply_markup=client_cancel_confirmation_keyboard(appointment_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("appt:cy:"))
async def confirm_client_cancellation(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    if callback.from_user is None:
        return
    try:
        appointment_id = UUID(callback.data.split(":", 2)[2])
    except (AttributeError, ValueError):
        await callback.answer("Некорректная запись", show_alert=True)
        return
    user = await get_or_create_telegram_user(db_session, callback.from_user)
    try:
        summary = await BookingService(_settings()).cancel_appointment(
            db_session,
            business_id=business_id,
            client_id=user.id,
            appointment_id=appointment_id,
        )
    except AppointmentNotFoundError:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    except AppointmentChangeNotAllowedError:
        await callback.answer(
            f"Отмена уже недоступна: до записи осталось меньше "
            f"{_settings().cancellation_cutoff_hours} ч.",
            show_alert=True,
        )
        return
    await state.clear()
    await _edit_or_answer(
        callback,
        "Запись отменена.\n\n"
        f"<b>{escape(summary.service_name)}</b>\n"
        f"{summary.local_start:%d.%m.%Y %H:%M}",
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer("Запись отменена")


@router.callback_query(F.data.startswith("appt:r:"))
async def start_client_reschedule(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    if callback.from_user is None:
        return
    try:
        appointment_id = UUID(callback.data.split(":", 2)[2])
    except (AttributeError, ValueError):
        await callback.answer("Некорректная запись", show_alert=True)
        return
    user = await get_or_create_telegram_user(db_session, callback.from_user)
    booking_service = BookingService(_settings())
    try:
        summary = await booking_service.get_appointment(
            db_session,
            business_id=business_id,
            client_id=user.id,
            appointment_id=appointment_id,
        )
    except AppointmentNotFoundError:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    if not summary.can_change:
        await callback.answer(
            f"Перенос доступен не позднее чем за "
            f"{_settings().cancellation_cutoff_hours} ч. до записи",
            show_alert=True,
        )
        return

    appointment = await db_session.scalar(
        select(Appointment).where(
            Appointment.id == appointment_id,
            Appointment.business_id == business_id,
            Appointment.client_id == user.id,
        )
    )
    entry = (
        await db_session.get(CalendarEntry, appointment.calendar_entry_id)
        if appointment is not None
        else None
    )
    if appointment is None or appointment.service_id is None or entry is None:
        await callback.answer("Перенос этой записи недоступен", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        appointment_id=str(appointment.id),
        client_id=str(user.id),
        service_id=str(appointment.service_id),
        master_id=str(entry.master_id),
    )
    await _show_reschedule_dates(callback, state, db_session, business_id)


@router.callback_query(F.data == "appt:rd")
async def back_to_reschedule_dates(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    await _show_reschedule_dates(callback, state, db_session, business_id)


@router.callback_query(
    BookingStates.rescheduling_date,
    F.data.startswith("rdate:"),
)
async def select_reschedule_date(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    try:
        local_date = date.fromisoformat(callback.data.split(":", 1)[1])
        data = await state.get_data()
        service_id = UUID(data["service_id"])
        master_id = UUID(data["master_id"])
        appointment_id = UUID(data["appointment_id"])
    except (AttributeError, KeyError, ValueError):
        await callback.answer("Начните перенос заново", show_alert=True)
        return
    try:
        slots = await AvailabilityService(_settings()).list_slots(
            db_session,
            business_id=business_id,
            master_id=master_id,
            service_id=service_id,
            local_date=local_date,
        )
    except BookingConfigurationError:
        await callback.answer("Расписание настроено некорректно", show_alert=True)
        return
    master = await db_session.get(Master, master_id)
    business = await db_session.get(Business, business_id)
    if master is None or business is None:
        await callback.answer("Перенос временно недоступен", show_alert=True)
        return
    timezone = ZoneInfo(master.timezone or business.timezone)
    await state.update_data(local_date=local_date.isoformat())
    if not slots:
        today = datetime.now(UTC).astimezone(timezone).date()
        dates = [
            today + timedelta(days=offset) for offset in range(_settings().booking_dates_shown)
        ]
        await _edit_or_answer(
            callback,
            "На эту дату свободных окон нет. Выберите другую дату:",
            reply_markup=dates_keyboard(
                dates,
                callback_prefix="rdate",
                back_callback=f"appt:v:{appointment_id}",
                back_text="Назад к записи",
            ),
        )
    else:
        await state.set_state(BookingStates.rescheduling_slot)
        await _edit_or_answer(
            callback,
            f"Свободное время на {local_date:%d.%m.%Y}:",
            reply_markup=slots_keyboard(
                slots,
                timezone,
                callback_prefix="rslot",
                back_callback="appt:rd",
            ),
        )
    await callback.answer()


@router.callback_query(
    BookingStates.rescheduling_slot,
    F.data.startswith("rslot:"),
)
async def select_reschedule_slot(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    try:
        service_start = datetime.fromtimestamp(
            int(callback.data.split(":", 1)[1]),
            tz=UTC,
        )
        data = await state.get_data()
        service_id = UUID(data["service_id"])
        master_id = UUID(data["master_id"])
        client_id = UUID(data["client_id"])
        local_date = date.fromisoformat(data["local_date"])
    except (AttributeError, KeyError, TypeError, ValueError):
        await callback.answer("Начните перенос заново", show_alert=True)
        return
    booking_service = BookingService(_settings())
    try:
        hold = await booking_service.create_hold(
            db_session,
            business_id=business_id,
            master_id=master_id,
            service_id=service_id,
            client_id=client_id,
            service_start=service_start,
            local_date=local_date,
        )
    except SlotUnavailableError:
        await callback.answer(
            "Это время уже занято. Выберите другое.",
            show_alert=True,
        )
        return
    await state.update_data(hold_id=str(hold.id))
    await state.set_state(BookingStates.rescheduling_confirming)
    summary = await booking_service.get_hold_summary(
        db_session,
        hold_id=hold.id,
        client_id=client_id,
    )
    await _edit_or_answer(
        callback,
        _format_reschedule_hold(summary),
        reply_markup=reschedule_confirmation_keyboard(),
    )
    await callback.answer()


@router.callback_query(
    BookingStates.rescheduling_confirming,
    F.data == "appt:rc",
)
async def confirm_client_reschedule(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    data = await state.get_data()
    booking_service = BookingService(_settings())
    try:
        hold_id = UUID(data["hold_id"])
        client_id = UUID(data["client_id"])
        summary = await booking_service.confirm_reschedule(
            db_session,
            business_id=business_id,
            client_id=client_id,
            appointment_id=UUID(data["appointment_id"]),
            hold_id=hold_id,
        )
    except (KeyError, ValueError, AppointmentNotFoundError):
        await state.clear()
        await callback.answer("Не удалось найти запись", show_alert=True)
        return
    except AppointmentChangeNotAllowedError:
        if "hold_id" in data and "client_id" in data:
            await booking_service.release_hold(
                db_session,
                hold_id=UUID(data["hold_id"]),
                client_id=UUID(data["client_id"]),
            )
        await state.clear()
        await callback.answer(
            "Срок самостоятельного переноса уже истёк",
            show_alert=True,
        )
        return
    except (HoldNotFoundError, HoldExpiredError):
        await state.clear()
        await callback.answer(
            "Время больше не удерживается. Начните перенос заново.",
            show_alert=True,
        )
        return
    await state.clear()
    await _edit_or_answer(
        callback,
        "Запись перенесена.\n\n" + _format_appointment(summary),
        reply_markup=client_appointment_actions_keyboard(summary),
    )
    await callback.answer("Новое время сохранено")


@router.callback_query(F.data == "appt:ra")
async def abort_client_reschedule(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    data = await state.get_data()
    booking_service = BookingService(_settings())
    if "hold_id" in data and "client_id" in data:
        await booking_service.release_hold(
            db_session,
            hold_id=UUID(data["hold_id"]),
            client_id=UUID(data["client_id"]),
        )
    try:
        summary = await booking_service.get_appointment(
            db_session,
            business_id=business_id,
            client_id=UUID(data["client_id"]),
            appointment_id=UUID(data["appointment_id"]),
        )
    except (KeyError, ValueError, AppointmentNotFoundError):
        await state.clear()
        await _edit_or_answer(
            callback,
            "Прежнее время записи сохранено.",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await state.clear()
        await _edit_or_answer(
            callback,
            "Прежнее время записи сохранено.\n\n" + _format_appointment(summary),
            reply_markup=client_appointment_actions_keyboard(summary),
        )
    await callback.answer()

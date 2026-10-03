import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from booking_bot.bot.conversation_ui import edit_or_answer
from booking_bot.bot.keyboards import (
    dates_keyboard,
    main_menu_keyboard,
    master_analytics_keyboard,
    master_appointment_actions_keyboard,
    master_appointments_keyboard,
    master_availability_keyboard,
    master_blocks_keyboard,
    master_days_off_keyboard,
    master_manual_cancel_keyboard,
    master_manual_comment_keyboard,
    master_manual_confirmation_keyboard,
    master_manual_dates_keyboard,
    master_manual_services_keyboard,
    master_manual_slots_keyboard,
    master_menu_keyboard,
    master_month_schedule_keyboard,
    master_notifications_keyboard,
    master_reschedule_confirmation_keyboard,
    master_schedule_dates_keyboard,
    master_service_actions_keyboard,
    master_service_cancel_keyboard,
    master_services_keyboard,
    master_weekdays_keyboard,
    slots_keyboard,
)
from booking_bot.bot.states import MasterStates
from booking_bot.config import get_settings
from booking_bot.db.models import (
    Appointment,
    Business,
    CalendarEntry,
    Master,
    Service,
    TelegramUser,
)
from booking_bot.domain.enums import AppointmentStatus
from booking_bot.domain.money import format_money
from booking_bot.services.analytics import (
    MasterAnalytics,
    MasterAnalyticsService,
    build_analytics_period,
)
from booking_bot.services.appointment_export import build_appointments_xlsx
from booking_bot.services.availability import AvailabilityService, BookingConfigurationError
from booking_bot.services.bookings import (
    AppointmentChangeNotAllowedError,
    BookingService,
    HoldExpiredError,
    HoldNotFoundError,
    ManualClientValidationError,
    SlotUnavailableError,
)
from booking_bot.services.bookings import (
    AppointmentNotFoundError as ClientAppointmentNotFoundError,
)
from booking_bot.services.conversations import ConversationService
from booking_bot.services.master_access import get_master_for_user
from booking_bot.services.master_schedule import (
    AppointmentNotFoundError,
    InvalidAppointmentTransitionError,
    MasterAppointment,
    MasterScheduleService,
    ScheduleConflictError,
)
from booking_bot.services.notification_delivery import (
    master_notifications_enabled,
    toggle_master_notifications,
)
from booking_bot.services.reminder_settings import (
    get_client_reminder_settings,
    update_client_reminder_settings,
)
from booking_bot.services.service_catalog import (
    MAX_BUFFER_MINUTES,
    MAX_DURATION_MINUTES,
    MAX_PRICE_MINOR,
    MIN_DURATION_MINUTES,
    DuplicateServiceNameError,
    InvalidServiceValueError,
    ServiceNotFoundError,
    SpecialistServiceCatalog,
)
from booking_bot.services.users import get_or_create_telegram_user, normalize_phone

router = Router(name="master")
schedule_service = MasterScheduleService()
service_catalog = SpecialistServiceCatalog()
manual_booking_service = BookingService(get_settings())
availability_service = AvailabilityService(get_settings())
analytics_service = MasterAnalyticsService()

STATUS_LABELS = {
    AppointmentStatus.PENDING_APPROVAL.value: "ожидает подтверждения",
    AppointmentStatus.PENDING_PAYMENT.value: "ожидает оплаты",
    AppointmentStatus.CONFIRMED.value: "подтверждена",
    AppointmentStatus.COMPLETED.value: "выполнена",
    AppointmentStatus.NO_SHOW.value: "клиент не пришёл",
    AppointmentStatus.CANCELLED_BY_MASTER.value: "отменена специалистом",
    AppointmentStatus.CANCELLED_BY_CLIENT.value: "отменена клиентом",
}
MONTH_APPOINTMENTS_PAGE_SIZE = 18
MONTH_STATUS_LABELS = {
    AppointmentStatus.PENDING_APPROVAL.value: "ожид.",
    AppointmentStatus.PENDING_PAYMENT.value: "оплата",
    AppointmentStatus.CONFIRMED.value: "подтв.",
    AppointmentStatus.COMPLETED.value: "готово",
    AppointmentStatus.NO_SHOW.value: "неявка",
}
STATUS_ACTIONS = {
    "approve": AppointmentStatus.CONFIRMED.value,
    "complete": AppointmentStatus.COMPLETED.value,
    "noshow": AppointmentStatus.NO_SHOW.value,
    "cancel": AppointmentStatus.CANCELLED_BY_MASTER.value,
}
TIME_RANGE_PATTERN = re.compile(r"^(?P<start>\d{2}:\d{2})\s*-\s*(?P<end>\d{2}:\d{2})$")
DATED_RANGE_PATTERN = re.compile(
    r"^(?P<date>\d{2}\.\d{2}\.\d{4})\s+"
    r"(?P<start>\d{2}:\d{2})\s*-\s*(?P<end>\d{2}:\d{2})"
    r"(?:\s+(?P<reason>.+))?$"
)
BUFFERS_PATTERN = re.compile(r"^(?P<before>\d{1,4})\s+(?P<after>\d{1,4})$")


async def _master_for_actor(
    actor,
    session: AsyncSession,
    business_id: UUID,
) -> tuple[TelegramUser, Master] | None:
    if actor is None:
        return None
    user = await get_or_create_telegram_user(session, actor)
    master = await get_master_for_user(
        session,
        business_id=business_id,
        user_id=user.id,
    )
    return (user, master) if master is not None else None


async def _master_timezone(
    session: AsyncSession,
    business_id: UUID,
    master: Master,
) -> ZoneInfo:
    business = await session.get(Business, business_id)
    if business is None:
        raise RuntimeError("Business not found")
    return ZoneInfo(master.timezone or business.timezone)


async def _edit(callback: CallbackQuery, text: str, **kwargs) -> None:
    await edit_or_answer(callback, text, **kwargs)


def _format_appointment(item: MasterAppointment) -> str:
    client_name = escape(item.client_name or "Клиент")
    phone = escape(item.client_phone or "не указан")
    location = f"\nМесто: <b>{escape(item.location_name)}</b>" if item.location_name else ""
    comment = f"\nКомментарий: {escape(item.client_comment)}" if item.client_comment else ""
    internal_note = (
        f"\nВнутренняя заметка: <i>{escape(item.internal_note)}</i>"
        if item.internal_note
        else "\nВнутренняя заметка: нет"
    )
    return (
        f"<b>{item.local_start:%d.%m.%Y %H:%M}-{item.local_end:%H:%M}</b>\n"
        f"Услуга: <b>{escape(item.service_name)}</b>\n"
        f"Длительность: <b>{item.duration_minutes} мин.</b>\n"
        f"Клиент: <b>{client_name}</b>\n"
        f"Телефон: <code>{phone}</code>\n"
        f"Статус: <b>{STATUS_LABELS.get(item.status, escape(item.status))}</b>"
        f"{location}{comment}{internal_note}"
    )


def _table_cell(value: str | None, width: int) -> str:
    normalized = " ".join((value or "—").split())
    if len(normalized) > width:
        normalized = normalized[: width - 1] + "…"
    return normalized.ljust(width)


def _format_month_appointments_table(
    appointments: list[MasterAppointment],
    *,
    period_start: date,
    period_end: date,
    page: int,
    page_size: int = MONTH_APPOINTMENTS_PAGE_SIZE,
) -> tuple[str, int, int]:
    page_count = max(1, (len(appointments) + page_size - 1) // page_size)
    page = min(max(page, 0), page_count - 1)
    start_index = page * page_size
    page_items = appointments[start_index : start_index + page_size]
    lines = [
        " № Дата  Время Клиент       Услуга         Статус",
        "── ───── ───── ──────────── ────────────── ──────",
    ]
    for index, item in enumerate(page_items, start_index + 1):
        lines.append(
            f"{index:>2} {item.local_start:%d.%m} {item.local_start:%H:%M} "
            f"{_table_cell(item.client_name, 12)} "
            f"{_table_cell(item.service_name, 14)} "
            f"{_table_cell(MONTH_STATUS_LABELS.get(item.status, item.status), 6)}"
        )
    table = escape("\n".join(lines))
    text = (
        "<b>Записи на ближайшие 30 дней</b>\n"
        f"Период: <b>{period_start:%d.%m.%Y}–{period_end:%d.%m.%Y}</b>\n"
        f"Всего записей: <b>{len(appointments)}</b>"
    )
    if appointments:
        text += f"\nСтраница: <b>{page + 1}/{page_count}</b>\n\n<pre>{table}</pre>"
    else:
        text += "\n\nЗаписей нет."
    return text, page, page_count


def _format_price(service: Service) -> str:
    if service.price_minor is None:
        return "не указана"
    return format_money(service.price_minor, service.currency)


def _format_money_minor(value: int | None, currency: str) -> str:
    if value is None:
        return "нет данных"
    return format_money(value, currency)


def _format_analytics(report: MasterAnalytics) -> str:
    cancellations = report.cancelled_by_client + report.cancelled_by_master
    cancellation_rate = cancellations / report.total_bookings * 100 if report.total_bookings else 0
    no_show_rate = report.no_shows / report.total_bookings * 100 if report.total_bookings else 0
    popular = (
        "\n".join(
            f"{index}. {escape(item.name)} — <b>{item.booking_count}</b>"
            for index, item in enumerate(report.popular_services, 1)
        )
        if report.popular_services
        else "Пока нет данных"
    )
    completed_value = _format_money_minor(
        report.completed_revenue_minor,
        report.currency,
    )
    average_value = _format_money_minor(
        report.average_completed_price_minor,
        report.currency,
    )
    active_value = _format_money_minor(report.active_value_minor, report.currency)
    return (
        "📊 <b>Статистика</b>\n"
        f"Период: <b>{report.period.label}</b>\n\n"
        f"Всего записей: <b>{report.total_bookings}</b>\n"
        f"Активные: <b>{report.active_bookings}</b>\n"
        f"Выполнены: <b>{report.completed_bookings}</b>\n"
        f"Отменены клиентами: <b>{report.cancelled_by_client}</b>\n"
        f"Отменены специалистом: <b>{report.cancelled_by_master}</b>\n"
        f"Неявки: <b>{report.no_shows}</b>\n"
        f"Доля отмен: <b>{cancellation_rate:.1f}%</b>\n"
        f"Доля неявок: <b>{no_show_rate:.1f}%</b>\n\n"
        f"Клиентов: <b>{report.distinct_clients}</b>\n"
        f"Ручных записей: <b>{report.manual_bookings}</b>\n\n"
        "<b>Стоимость записей</b>\n"
        f"Выполненные: <b>{completed_value}</b>\n"
        f"Средняя выполненная: <b>{average_value}</b>\n"
        f"Активные: <b>{active_value}</b>\n\n"
        "<b>Популярные услуги</b>\n"
        f"{popular}\n\n"
        "<i>Суммы рассчитаны по ценам записей и пока не подтверждают фактическую оплату.</i>"
    )


def _format_service(service: Service) -> str:
    status = "опубликована" if service.is_active else "скрыта от клиентов"
    approval = "требуется подтверждение" if service.requires_approval else "автоматическое"
    description = escape(service.description) if service.description else "не указано"
    return (
        f"<b>{escape(service.name)}</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Описание: {description}\n"
        f"Цена: <b>{_format_price(service)}</b>\n"
        f"Длительность: <b>{service.duration_minutes} мин.</b>\n"
        f"Перерыв до: <b>{service.buffer_before_minutes} мин.</b>\n"
        f"Перерыв после: <b>{service.buffer_after_minutes} мин.</b>\n"
        f"Подтверждение: <b>{approval}</b>"
    )


def _parse_price_minor(value: str) -> int | None:
    normalized = value.strip().lower()
    if normalized in {"-", "нет", "не указана", "не указано", "без цены"}:
        return None
    normalized = re.sub(r"\s*(?:₽|rub|руб(?:\.|лей|ля)?)\s*$", "", normalized)
    normalized = normalized.replace(" ", "").replace(",", ".")
    if re.fullmatch(r"\d+(?:\.\d{1,2})?", normalized) is None:
        raise InvalidServiceValueError("Invalid price")
    try:
        price_minor = int(Decimal(normalized) * 100)
    except (InvalidOperation, ValueError):
        raise InvalidServiceValueError("Invalid price") from None
    if not 0 <= price_minor <= MAX_PRICE_MINOR:
        raise InvalidServiceValueError("Invalid price")
    return price_minor


async def _service_from_state(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    business_id: UUID,
) -> tuple[Master, Service] | None:
    context = await _master_for_actor(message.from_user, session, business_id)
    if context is None:
        await state.clear()
        await message.answer("Кабинет специалиста недоступен.")
        return None
    _user, master = context
    try:
        service_id = UUID((await state.get_data())["service_id"])
        service = await service_catalog.get_service(
            session,
            business_id=business_id,
            master_id=master.id,
            service_id=service_id,
        )
    except (KeyError, ValueError, ServiceNotFoundError):
        await state.clear()
        await message.answer(
            "Услуга не найдена.",
            reply_markup=master_services_keyboard([]),
        )
        return None
    return master, service


async def _answer_service(message: Message, service: Service) -> None:
    await message.answer(
        _format_service(service),
        reply_markup=master_service_actions_keyboard(service),
    )


async def _deny(callback: CallbackQuery) -> None:
    await callback.answer("Кабинет доступен только владельцу бота", show_alert=True)


async def _release_manual_hold(state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    try:
        hold_id = UUID(data["hold_id"])
        holder_user_id = UUID(data["client_id"])
    except (KeyError, ValueError):
        return
    await manual_booking_service.release_hold(
        session,
        hold_id=hold_id,
        client_id=holder_user_id,
    )


async def _show_manual_dates(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    business_id: UUID,
    master: Master,
) -> None:
    timezone = await _master_timezone(session, business_id, master)
    today = datetime.now(UTC).astimezone(timezone).date()
    dates = [today + timedelta(days=offset) for offset in range(get_settings().booking_dates_shown)]
    await state.set_state(MasterStates.manual_selecting_date)
    await _edit(
        callback,
        "Выберите дату ручной записи:",
        reply_markup=master_manual_dates_keyboard(dates),
    )
    await callback.answer()


async def _show_master_reschedule_dates(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    business_id: UUID,
    master: Master,
) -> None:
    data = await state.get_data()
    try:
        appointment_id = UUID(data["appointment_id"])
    except (KeyError, ValueError):
        await state.clear()
        await callback.answer("Начните перенос заново", show_alert=True)
        return
    timezone = await _master_timezone(session, business_id, master)
    today = datetime.now(UTC).astimezone(timezone).date()
    dates = [today + timedelta(days=offset) for offset in range(get_settings().booking_dates_shown)]
    await state.set_state(MasterStates.rescheduling_date)
    await _edit(
        callback,
        "Выберите новую дату записи:",
        reply_markup=dates_keyboard(
            dates,
            callback_prefix="mrd",
            back_callback=f"master:appointment:{appointment_id}",
            back_text="Назад к записи",
        ),
    )
    await callback.answer()


def _normalize_phone(raw_phone: str) -> str | None:
    return normalize_phone(raw_phone)


def _format_manual_confirmation(
    *,
    service_name: str,
    local_start: datetime,
    local_end: datetime,
    location_name: str | None,
    client_name: str,
    client_phone: str,
    client_comment: str | None,
) -> str:
    location = f"\nМесто: <b>{escape(location_name)}</b>" if location_name else ""
    comment = f"\nКомментарий: {escape(client_comment)}" if client_comment else "\nКомментарий: нет"
    return (
        "<b>Проверьте ручную запись</b>\n\n"
        f"Услуга: <b>{escape(service_name)}</b>\n"
        f"Дата: <b>{local_start:%d.%m.%Y}</b>\n"
        f"Время: <b>{local_start:%H:%M}-{local_end:%H:%M}</b>"
        f"{location}\n"
        f"Клиент: <b>{escape(client_name)}</b>\n"
        f"Телефон: <code>{escape(client_phone)}</code>"
        f"{comment}\n\n"
        "Клиент добавлен вручную, поэтому Telegram-напоминания ему не отправляются."
    )


@router.message(Command("cabinet"))
async def master_cabinet_command(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(message.from_user, db_session, business_id)
    if context is None:
        await message.answer(
            "Кабинет доступен только привязанному специалисту.",
            reply_markup=main_menu_keyboard(),
        )
        return
    _user, master = context
    await _release_manual_hold(state, db_session)
    await state.clear()
    await message.answer(
        f"Мой кабинет — <b>{escape(master.display_name)}</b>:",
        reply_markup=master_menu_keyboard(
            unread_count=await ConversationService().total_unread(
                db_session,
                business_id=business_id,
                actor_user_id=_user.id,
            )
        ),
    )


@router.callback_query(F.data == "master:menu")
async def master_menu(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    await _release_manual_hold(state, db_session)
    await state.clear()
    await _edit(
        callback,
        f"Мой кабинет — <b>{escape(master.display_name)}</b>:",
        reply_markup=master_menu_keyboard(
            unread_count=await ConversationService().total_unread(
                db_session,
                business_id=business_id,
                actor_user_id=_user.id,
            )
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "master:client-menu")
async def master_client_menu(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    await state.clear()
    await _edit(
        callback,
        "Клиентское меню:",
        reply_markup=main_menu_keyboard(master_access=True),
    )
    await callback.answer()


@router.callback_query(F.data == "master:services")
async def master_services(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    await state.clear()
    services = await service_catalog.list_services(
        db_session,
        business_id=business_id,
        master_id=master.id,
    )
    text = (
        "<b>Услуги и цены</b>\n\n"
        "✅ — доступна клиентам\n"
        "⏸ — скрыта\n\n"
        "Выберите услугу для настройки."
        if services
        else "<b>Услуги и цены</b>\n\nУслуг пока нет."
    )
    await _edit(callback, text, reply_markup=master_services_keyboard(services))
    await callback.answer()


@router.callback_query(F.data == "svc:new")
async def master_create_service(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    if await _master_for_actor(callback.from_user, db_session, business_id) is None:
        await _deny(callback)
        return
    await state.clear()
    await state.set_state(MasterStates.waiting_service_create_name)
    await _edit(
        callback,
        "Отправьте название новой услуги.\n\n"
        "Она будет создана скрытой. Настройте параметры, а затем опубликуйте её.",
        reply_markup=master_service_cancel_keyboard(),
    )
    await callback.answer()


@router.message(MasterStates.waiting_service_create_name, F.text)
async def master_receive_new_service_name(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(message.from_user, db_session, business_id)
    if context is None:
        await state.clear()
        await message.answer("Кабинет специалиста недоступен.")
        return
    _user, master = context
    try:
        service = await service_catalog.create_service(
            db_session,
            business_id=business_id,
            master_id=master.id,
            name=message.text or "",
        )
    except DuplicateServiceNameError:
        await message.answer("Услуга с таким названием уже существует.")
        return
    except InvalidServiceValueError:
        await message.answer("Название должно содержать от 2 до 160 символов.")
        return
    await state.clear()
    await message.answer("Услуга создана. Заполните параметры и нажмите «Опубликовать услугу».")
    await _answer_service(message, service)


@router.callback_query(F.data.startswith("svc:v:"))
async def master_service_details(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    try:
        service_id = UUID((callback.data or "").rsplit(":", 1)[-1])
        service = await service_catalog.get_service(
            db_session,
            business_id=business_id,
            master_id=master.id,
            service_id=service_id,
        )
    except (ValueError, ServiceNotFoundError):
        await callback.answer("Услуга не найдена", show_alert=True)
        return
    await state.clear()
    await _edit(
        callback,
        _format_service(service),
        reply_markup=master_service_actions_keyboard(service),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("svc:e:"))
async def master_edit_service(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    try:
        _prefix, _edit_action, field, service_raw = (callback.data or "").split(":")
        service_id = UUID(service_raw)
        await service_catalog.get_service(
            db_session,
            business_id=business_id,
            master_id=master.id,
            service_id=service_id,
        )
        state_and_prompt = {
            "n": (
                MasterStates.waiting_service_name,
                "Отправьте новое название услуги:",
            ),
            "d": (
                MasterStates.waiting_service_description,
                "Отправьте новое описание. Чтобы удалить описание, отправьте <code>-</code>.",
            ),
            "t": (
                MasterStates.waiting_service_duration,
                f"Отправьте длительность в минутах: от {MIN_DURATION_MINUTES} "
                f"до {MAX_DURATION_MINUTES}.",
            ),
            "p": (
                MasterStates.waiting_service_price,
                "Отправьте цену в рублях, например <code>3500</code> или "
                "<code>3500,50</code>.\nЧтобы скрыть цену, отправьте <code>-</code>.",
            ),
            "b": (
                MasterStates.waiting_service_buffers,
                "Отправьте два числа через пробел: перерыв до и после услуги в минутах.\n"
                "Например: <code>15 30</code>.",
            ),
        }
        target_state, prompt = state_and_prompt[field]
    except (ValueError, KeyError, ServiceNotFoundError):
        await callback.answer("Услуга или поле не найдены", show_alert=True)
        return
    await state.clear()
    await state.update_data(service_id=str(service_id))
    await state.set_state(target_state)
    await _edit(
        callback,
        prompt,
        reply_markup=master_service_cancel_keyboard(service_id),
    )
    await callback.answer()


@router.message(MasterStates.waiting_service_name, F.text)
async def master_receive_service_name(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _service_from_state(message, state, db_session, business_id)
    if context is None:
        return
    master, service = context
    try:
        service = await service_catalog.set_name(
            db_session,
            business_id=business_id,
            master_id=master.id,
            service_id=service.id,
            name=message.text or "",
        )
    except DuplicateServiceNameError:
        await message.answer("Услуга с таким названием уже существует.")
        return
    except InvalidServiceValueError:
        await message.answer("Название должно содержать от 2 до 160 символов.")
        return
    await state.clear()
    await _answer_service(message, service)


@router.message(MasterStates.waiting_service_description, F.text)
async def master_receive_service_description(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _service_from_state(message, state, db_session, business_id)
    if context is None:
        return
    master, service = context
    raw_description = (message.text or "").strip()
    description = (
        None if raw_description.lower() in {"-", "нет", "без описания"} else raw_description
    )
    try:
        service = await service_catalog.set_description(
            db_session,
            business_id=business_id,
            master_id=master.id,
            service_id=service.id,
            description=description,
        )
    except InvalidServiceValueError:
        await message.answer("Описание не должно быть длиннее 2000 символов.")
        return
    await state.clear()
    await _answer_service(message, service)


@router.message(MasterStates.waiting_service_duration, F.text)
async def master_receive_service_duration(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _service_from_state(message, state, db_session, business_id)
    if context is None:
        return
    master, service = context
    try:
        duration = int((message.text or "").strip())
        service = await service_catalog.set_duration(
            db_session,
            business_id=business_id,
            master_id=master.id,
            service_id=service.id,
            duration_minutes=duration,
        )
    except (ValueError, InvalidServiceValueError):
        await message.answer(
            f"Введите целое число от {MIN_DURATION_MINUTES} до {MAX_DURATION_MINUTES}."
        )
        return
    await state.clear()
    await _answer_service(message, service)


@router.message(MasterStates.waiting_service_price, F.text)
async def master_receive_service_price(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _service_from_state(message, state, db_session, business_id)
    if context is None:
        return
    master, service = context
    try:
        price_minor = _parse_price_minor(message.text or "")
        service = await service_catalog.set_price(
            db_session,
            business_id=business_id,
            master_id=master.id,
            service_id=service.id,
            price_minor=price_minor,
        )
    except InvalidServiceValueError:
        await message.answer("Введите корректную цену, например <code>3500</code>, или «-».")
        return
    await state.clear()
    await _answer_service(message, service)


@router.message(MasterStates.waiting_service_buffers, F.text)
async def master_receive_service_buffers(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _service_from_state(message, state, db_session, business_id)
    if context is None:
        return
    master, service = context
    match = BUFFERS_PATTERN.fullmatch((message.text or "").strip())
    if match is None:
        await message.answer("Отправьте два целых числа, например <code>15 30</code>.")
        return
    try:
        service = await service_catalog.set_buffers(
            db_session,
            business_id=business_id,
            master_id=master.id,
            service_id=service.id,
            buffer_before_minutes=int(match.group("before")),
            buffer_after_minutes=int(match.group("after")),
        )
    except InvalidServiceValueError:
        await message.answer(f"Каждый перерыв должен быть от 0 до {MAX_BUFFER_MINUTES} минут.")
        return
    await state.clear()
    await _answer_service(message, service)


@router.callback_query(F.data.startswith("svc:t:"))
async def master_toggle_service_setting(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    try:
        _prefix, _toggle, setting, service_raw = (callback.data or "").split(":")
        service_id = UUID(service_raw)
        if setting == "r":
            service = await service_catalog.toggle_approval(
                db_session,
                business_id=business_id,
                master_id=master.id,
                service_id=service_id,
            )
            result_text = (
                "Ручное подтверждение включено"
                if service.requires_approval
                else "Автоподтверждение включено"
            )
        elif setting == "a":
            service = await service_catalog.toggle_active(
                db_session,
                business_id=business_id,
                master_id=master.id,
                service_id=service_id,
            )
            result_text = "Услуга опубликована" if service.is_active else "Услуга скрыта"
        else:
            raise ValueError
    except (ValueError, ServiceNotFoundError):
        await callback.answer("Услуга не найдена", show_alert=True)
        return
    await state.clear()
    await _edit(
        callback,
        _format_service(service),
        reply_markup=master_service_actions_keyboard(service),
    )
    await callback.answer(result_text)


@router.callback_query(F.data == "mb:start")
async def master_manual_booking_start(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    await _release_manual_hold(state, db_session)
    await state.clear()
    services = [
        service
        for service in await service_catalog.list_services(
            db_session,
            business_id=business_id,
            master_id=master.id,
        )
        if service.is_active
    ]
    if not services:
        await callback.answer("Сначала опубликуйте хотя бы одну услугу", show_alert=True)
        return
    await state.set_state(MasterStates.manual_selecting_service)
    await _edit(
        callback,
        "<b>Новая ручная запись</b>\n\nВыберите услугу:",
        reply_markup=master_manual_services_keyboard(services),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("master:analytics:"))
async def master_analytics(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    mode = (callback.data or "").rsplit(":", 1)[-1]
    try:
        timezone = await _master_timezone(db_session, business_id, master)
        period = build_analytics_period(mode=mode, timezone=timezone)
        report = await analytics_service.build_report(
            db_session,
            business_id=business_id,
            master=master,
            period=period,
        )
    except ValueError:
        await callback.answer("Неизвестный период", show_alert=True)
        return
    await _release_manual_hold(state, db_session)
    await state.clear()
    await _edit(
        callback,
        _format_analytics(report),
        reply_markup=master_analytics_keyboard(mode),
    )
    await callback.answer()


@router.callback_query(
    MasterStates.manual_selecting_service,
    F.data.startswith("mb:s:"),
)
async def master_manual_select_service(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    user, master = context
    try:
        service_id = UUID((callback.data or "").rsplit(":", 1)[-1])
        service = await service_catalog.get_service(
            db_session,
            business_id=business_id,
            master_id=master.id,
            service_id=service_id,
        )
    except (ValueError, ServiceNotFoundError):
        await callback.answer("Услуга не найдена", show_alert=True)
        return
    if not service.is_active:
        await callback.answer("Эта услуга сейчас скрыта", show_alert=True)
        return
    await state.update_data(
        service_id=str(service.id),
        master_id=str(master.id),
        actor_user_id=str(user.id),
    )
    await _show_manual_dates(callback, state, db_session, business_id, master)


@router.callback_query(F.data == "mb:dates")
async def master_manual_back_to_dates(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    await _show_manual_dates(callback, state, db_session, business_id, master)


@router.callback_query(
    MasterStates.manual_selecting_date,
    F.data.startswith("mb:d:"),
)
async def master_manual_select_date(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    try:
        local_date = date.fromisoformat((callback.data or "").rsplit(":", 1)[-1])
        service_id = UUID((await state.get_data())["service_id"])
        slots = await availability_service.list_slots(
            db_session,
            business_id=business_id,
            master_id=master.id,
            service_id=service_id,
            local_date=local_date,
            respect_min_lead_time=False,
        )
    except (KeyError, ValueError):
        await callback.answer("Начните создание записи заново", show_alert=True)
        return
    except BookingConfigurationError:
        await callback.answer("Услуга или расписание сейчас недоступны", show_alert=True)
        return
    timezone = await _master_timezone(db_session, business_id, master)
    await state.update_data(local_date=local_date.isoformat())
    if not slots:
        await _edit(
            callback,
            f"На {local_date:%d.%m.%Y} свободных окон нет. Выберите другую дату:",
            reply_markup=master_manual_dates_keyboard(
                [
                    datetime.now(UTC).astimezone(timezone).date() + timedelta(days=offset)
                    for offset in range(get_settings().booking_dates_shown)
                ]
            ),
        )
    else:
        await state.set_state(MasterStates.manual_selecting_slot)
        await _edit(
            callback,
            f"Свободное время на {local_date:%d.%m.%Y}:",
            reply_markup=master_manual_slots_keyboard(slots, timezone),
        )
    await callback.answer()


@router.callback_query(
    MasterStates.manual_selecting_slot,
    F.data.startswith("mb:t:"),
)
async def master_manual_select_slot(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    user, master = context
    try:
        data = await state.get_data()
        service_start = datetime.fromtimestamp(
            int((callback.data or "").rsplit(":", 1)[-1]),
            tz=UTC,
        )
        service_id = UUID(data["service_id"])
        local_date = date.fromisoformat(data["local_date"])
    except (KeyError, TypeError, ValueError):
        await callback.answer("Начните создание записи заново", show_alert=True)
        return
    try:
        hold = await manual_booking_service.create_hold(
            db_session,
            business_id=business_id,
            master_id=master.id,
            service_id=service_id,
            client_id=user.id,
            service_start=service_start,
            local_date=local_date,
            respect_min_lead_time=False,
        )
    except SlotUnavailableError:
        await callback.answer("Это время уже занято. Выберите другое.", show_alert=True)
        return
    await state.update_data(hold_id=str(hold.id), client_id=str(user.id))
    await state.set_state(MasterStates.manual_waiting_name)
    await _edit(
        callback,
        "Время удерживается 10 минут.\n\nОтправьте имя клиента:",
        reply_markup=master_manual_cancel_keyboard(),
    )
    await callback.answer()


@router.message(MasterStates.manual_waiting_name, F.text)
async def master_manual_receive_name(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    if await _master_for_actor(message.from_user, db_session, business_id) is None:
        await state.clear()
        await message.answer("Кабинет специалиста недоступен.")
        return
    client_name = " ".join((message.text or "").split())
    if not 2 <= len(client_name) <= 160:
        await message.answer("Имя должно содержать от 2 до 160 символов.")
        return
    await state.update_data(client_name=client_name)
    await state.set_state(MasterStates.manual_waiting_phone)
    await message.answer(
        "Отправьте телефон клиента:",
        reply_markup=master_manual_cancel_keyboard(),
    )


@router.message(MasterStates.manual_waiting_phone, F.text)
async def master_manual_receive_phone(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    if await _master_for_actor(message.from_user, db_session, business_id) is None:
        await state.clear()
        await message.answer("Кабинет специалиста недоступен.")
        return
    client_phone = _normalize_phone(message.text or "")
    if client_phone is None:
        await message.answer("Введите телефон из 10-15 цифр.")
        return
    await state.update_data(client_phone=client_phone)
    await state.set_state(MasterStates.manual_waiting_comment)
    await message.answer(
        "Отправьте комментарий к записи или нажмите «Без комментария»:",
        reply_markup=master_manual_comment_keyboard(),
    )


async def _manual_confirmation_from_state(
    state: FSMContext,
    session: AsyncSession,
) -> tuple[str, dict]:
    data = await state.get_data()
    hold_summary = await manual_booking_service.get_hold_summary(
        session,
        hold_id=UUID(data["hold_id"]),
        client_id=UUID(data["client_id"]),
    )
    text = _format_manual_confirmation(
        service_name=hold_summary.service_name,
        local_start=hold_summary.local_start,
        local_end=hold_summary.local_end,
        location_name=hold_summary.location_name,
        client_name=data["client_name"],
        client_phone=data["client_phone"],
        client_comment=data.get("client_comment"),
    )
    return text, data


@router.message(MasterStates.manual_waiting_comment, F.text)
async def master_manual_receive_comment(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    if await _master_for_actor(message.from_user, db_session, business_id) is None:
        await state.clear()
        await message.answer("Кабинет специалиста недоступен.")
        return
    comment = (message.text or "").strip()
    if len(comment) > 2000:
        await message.answer("Комментарий должен быть не длиннее 2000 символов.")
        return
    await state.update_data(client_comment=comment or None)
    try:
        text, _data = await _manual_confirmation_from_state(state, db_session)
    except (KeyError, ValueError, HoldNotFoundError):
        await state.clear()
        await message.answer("Время больше не удерживается. Создайте запись заново.")
        return
    await state.set_state(MasterStates.manual_confirming)
    await message.answer(text, reply_markup=master_manual_confirmation_keyboard())


@router.callback_query(
    MasterStates.manual_waiting_comment,
    F.data == "mb:comment:skip",
)
async def master_manual_skip_comment(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    if await _master_for_actor(callback.from_user, db_session, business_id) is None:
        await _deny(callback)
        return
    await state.update_data(client_comment=None)
    try:
        text, _data = await _manual_confirmation_from_state(state, db_session)
    except (KeyError, ValueError, HoldNotFoundError):
        await state.clear()
        await callback.answer("Время больше не удерживается", show_alert=True)
        return
    await state.set_state(MasterStates.manual_confirming)
    await _edit(
        callback,
        text,
        reply_markup=master_manual_confirmation_keyboard(),
    )
    await callback.answer()


@router.callback_query(
    MasterStates.manual_confirming,
    F.data == "mb:confirm",
)
async def master_manual_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    user, master = context
    data = await state.get_data()
    try:
        summary = await manual_booking_service.confirm_manual_hold(
            db_session,
            business_id=business_id,
            hold_id=UUID(data["hold_id"]),
            holder_user_id=UUID(data["client_id"]),
            actor_user_id=user.id,
            client_name=data["client_name"],
            client_phone=data["client_phone"],
            client_comment=data.get("client_comment"),
        )
        appointment = await schedule_service.get_appointment(
            db_session,
            business_id=business_id,
            master=master,
            appointment_id=summary.appointment_id,
        )
    except (KeyError, ValueError, HoldNotFoundError, ManualClientValidationError):
        await state.clear()
        await callback.answer("Не удалось создать запись", show_alert=True)
        return
    except HoldExpiredError:
        await state.clear()
        await _edit(
            callback,
            "Время удержания истекло. Создайте запись заново.",
            reply_markup=master_menu_keyboard(),
        )
        await callback.answer()
        return
    await state.clear()
    await _edit(
        callback,
        "✅ <b>Запись создана вручную</b>\n\n" + _format_appointment(appointment),
        reply_markup=master_appointment_actions_keyboard(appointment),
    )
    await callback.answer("Запись добавлена в расписание")


@router.callback_query(F.data == "mb:abort")
async def master_manual_abort(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    await _release_manual_hold(state, db_session)
    await state.clear()
    await _edit(
        callback,
        f"Мой кабинет — <b>{escape(master.display_name)}</b>:",
        reply_markup=master_menu_keyboard(),
    )
    await callback.answer("Создание записи отменено")


@router.callback_query(F.data == "master:month:export")
async def master_month_export(
    callback: CallbackQuery,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    message = callback.message if isinstance(callback.message, Message) else None
    if message is None:
        await callback.answer("Не удалось отправить файл", show_alert=True)
        return

    timezone = await _master_timezone(db_session, business_id, master)
    period_start = datetime.now(UTC).astimezone(timezone).date()
    period_end = period_start + timedelta(days=29)
    appointments = await schedule_service.list_appointments(
        db_session,
        business_id=business_id,
        master=master,
        start_date=period_start,
        days=30,
    )
    document = BufferedInputFile(
        build_appointments_xlsx(
            appointments,
            specialist_name=master.display_name,
            period_start=period_start,
            period_end=period_end,
        ),
        filename=f"appointments-{period_start:%Y-%m-%d}-{period_end:%Y-%m-%d}.xlsx",
    )
    await message.answer_document(
        document,
        caption=(
            f"Записи на период {period_start:%d.%m.%Y}–{period_end:%d.%m.%Y}. "
            f"Всего: {len(appointments)}."
        ),
    )
    await callback.answer("Excel-файл готов")


@router.callback_query(F.data.startswith("master:month:page:"))
async def master_month_schedule(
    callback: CallbackQuery,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    try:
        requested_page = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Некорректная страница", show_alert=True)
        return

    timezone = await _master_timezone(db_session, business_id, master)
    period_start = datetime.now(UTC).astimezone(timezone).date()
    period_end = period_start + timedelta(days=29)
    appointments = await schedule_service.list_appointments(
        db_session,
        business_id=business_id,
        master=master,
        start_date=period_start,
        days=30,
    )
    text, page, page_count = _format_month_appointments_table(
        appointments,
        period_start=period_start,
        period_end=period_end,
        page=requested_page,
    )
    await _edit(
        callback,
        text,
        reply_markup=master_month_schedule_keyboard(
            page=page,
            page_count=page_count,
        ),
    )
    await callback.answer()


@router.callback_query(
    F.data.in_(
        {
            "master:schedule:today",
            "master:schedule:tomorrow",
            "master:schedule:week",
        }
    )
)
async def master_schedule(
    callback: CallbackQuery,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    mode = (callback.data or "").rsplit(":", 1)[-1]
    timezone = await _master_timezone(db_session, business_id, master)
    today = datetime.now(UTC).astimezone(timezone).date()
    if mode == "today":
        start_date, days, title = today, 1, "Расписание на сегодня"
    elif mode == "tomorrow":
        start_date, days, title = today + timedelta(days=1), 1, "Расписание на завтра"
    elif mode == "week":
        start_date, days, title = today, 7, "Расписание на 7 дней"
    else:
        await callback.answer("Неизвестный период", show_alert=True)
        return

    appointments = await schedule_service.list_appointments(
        db_session,
        business_id=business_id,
        master=master,
        start_date=start_date,
        days=days,
    )
    text = (
        f"<b>{title}</b>\n\nВыберите запись для просмотра."
        if appointments
        else f"<b>{title}</b>\n\nЗаписей нет."
    )
    await _edit(
        callback,
        text,
        reply_markup=master_appointments_keyboard(appointments),
    )
    await callback.answer()


@router.callback_query(F.data == "master:schedule:choose")
async def master_choose_schedule_date(
    callback: CallbackQuery,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    timezone = await _master_timezone(db_session, business_id, master)
    today = datetime.now(UTC).astimezone(timezone).date()
    dates = [today + timedelta(days=offset) for offset in range(60)]
    await _edit(
        callback,
        "<b>Расписание на дату</b>\n\nВыберите день:",
        reply_markup=master_schedule_dates_keyboard(dates),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("master:schedule:date:"))
async def master_schedule_selected_date(
    callback: CallbackQuery,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    try:
        selected_date = date.fromisoformat((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Некорректная дата", show_alert=True)
        return
    appointments = await schedule_service.list_appointments(
        db_session,
        business_id=business_id,
        master=master,
        start_date=selected_date,
        days=1,
    )
    text = f"<b>Расписание на {selected_date:%d.%m.%Y}</b>\n\n" + (
        "Выберите запись для просмотра." if appointments else "Записей нет."
    )
    await _edit(
        callback,
        text,
        reply_markup=master_appointments_keyboard(appointments),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("master:appointment:"))
async def master_appointment(
    callback: CallbackQuery,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    try:
        appointment_id = UUID((callback.data or "").rsplit(":", 1)[-1])
        appointment = await schedule_service.get_appointment(
            db_session,
            business_id=business_id,
            master=master,
            appointment_id=appointment_id,
        )
    except (ValueError, AppointmentNotFoundError):
        await callback.answer("Запись не найдена", show_alert=True)
        return
    await _edit(
        callback,
        _format_appointment(appointment),
        reply_markup=master_appointment_actions_keyboard(appointment),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("master:duration:"))
async def master_change_duration(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    try:
        appointment_id = UUID((callback.data or "").rsplit(":", 1)[-1])
        appointment = await schedule_service.get_appointment(
            db_session,
            business_id=business_id,
            master=master,
            appointment_id=appointment_id,
        )
    except (ValueError, AppointmentNotFoundError):
        await callback.answer("Запись не найдена", show_alert=True)
        return
    await state.set_state(MasterStates.waiting_appointment_duration)
    await state.update_data(appointment_id=str(appointment_id))
    await _edit(
        callback,
        f"Текущая длительность: <b>{appointment.duration_minutes} мин.</b>\n\n"
        "Отправьте новую длительность в минутах (от 5 до 1440).",
    )
    await callback.answer()


@router.message(MasterStates.waiting_appointment_duration, F.text)
async def master_receive_duration(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(message.from_user, db_session, business_id)
    if context is None:
        await state.clear()
        await message.answer("Кабинет специалиста недоступен.")
        return
    user, master = context
    data = await state.get_data()
    try:
        duration_minutes = int((message.text or "").strip())
        appointment = await schedule_service.change_appointment_duration(
            db_session,
            business_id=business_id,
            master=master,
            appointment_id=UUID(data["appointment_id"]),
            actor_user_id=user.id,
            duration_minutes=duration_minutes,
        )
    except (KeyError, ValueError):
        await message.answer("Введите целое число от 5 до 1440.")
        return
    except AppointmentNotFoundError:
        await state.clear()
        await message.answer("Запись не найдена.", reply_markup=master_menu_keyboard())
        return
    except ScheduleConflictError:
        await message.answer(
            "Новая длительность пересекается с другой записью или блокировкой. "
            "Укажите меньшее значение."
        )
        return
    await state.clear()
    await message.answer(
        "Длительность изменена.\n\n" + _format_appointment(appointment),
        reply_markup=master_appointment_actions_keyboard(appointment),
    )


@router.callback_query(F.data.startswith("master:note:"))
async def master_edit_internal_note(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    try:
        appointment_id = UUID((callback.data or "").rsplit(":", 1)[-1])
        appointment = await schedule_service.get_appointment(
            db_session,
            business_id=business_id,
            master=master,
            appointment_id=appointment_id,
        )
    except (ValueError, AppointmentNotFoundError):
        await callback.answer("Запись не найдена", show_alert=True)
        return
    current = escape(appointment.internal_note) if appointment.internal_note else "нет"
    await state.set_state(MasterStates.waiting_internal_note)
    await state.update_data(appointment_id=str(appointment_id))
    await _edit(
        callback,
        f"Текущая внутренняя заметка: <i>{current}</i>\n\n"
        "Отправьте новый текст. Чтобы удалить заметку, отправьте один символ <code>-</code>.\n"
        "Клиент эту заметку не увидит.",
    )
    await callback.answer()


@router.message(MasterStates.waiting_internal_note, F.text)
async def master_receive_internal_note(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(message.from_user, db_session, business_id)
    if context is None:
        await state.clear()
        await message.answer("Кабинет специалиста недоступен.")
        return
    user, master = context
    data = await state.get_data()
    note = None if (message.text or "").strip() == "-" else message.text
    try:
        appointment = await schedule_service.set_internal_note(
            db_session,
            business_id=business_id,
            master=master,
            appointment_id=UUID(data["appointment_id"]),
            actor_user_id=user.id,
            note=note,
        )
    except KeyError:
        await state.clear()
        await message.answer("Запись не найдена.", reply_markup=master_menu_keyboard())
        return
    except ValueError:
        await message.answer("Заметка должна быть не длиннее 4000 символов.")
        return
    except AppointmentNotFoundError:
        await state.clear()
        await message.answer("Запись не найдена.", reply_markup=master_menu_keyboard())
        return
    await state.clear()
    await message.answer(
        "Внутренняя заметка сохранена.\n\n" + _format_appointment(appointment),
        reply_markup=master_appointment_actions_keyboard(appointment),
    )


@router.callback_query(F.data.startswith("master:move-start:"))
async def master_start_reschedule(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    user, master = context
    try:
        appointment_id = UUID((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    appointment = await db_session.scalar(
        select(Appointment)
        .join(CalendarEntry, CalendarEntry.id == Appointment.calendar_entry_id)
        .where(
            Appointment.id == appointment_id,
            Appointment.business_id == business_id,
            CalendarEntry.master_id == master.id,
            Appointment.status.in_(
                [
                    AppointmentStatus.PENDING_APPROVAL.value,
                    AppointmentStatus.CONFIRMED.value,
                ]
            ),
        )
    )
    if (
        appointment is None
        or appointment.service_id is None
        or appointment.service_starts_at <= datetime.now(UTC)
    ):
        await callback.answer("Эту запись уже нельзя перенести", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        appointment_id=str(appointment.id),
        service_id=str(appointment.service_id),
        duration_minutes=appointment.duration_minutes,
        master_id=str(master.id),
        holder_user_id=str(user.id),
    )
    await _show_master_reschedule_dates(
        callback,
        state,
        db_session,
        business_id,
        master,
    )


@router.callback_query(F.data == "master:move:dates")
async def master_back_to_reschedule_dates(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    await _show_master_reschedule_dates(
        callback,
        state,
        db_session,
        business_id,
        master,
    )


@router.callback_query(MasterStates.rescheduling_date, F.data.startswith("mrd:"))
async def master_select_reschedule_date(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    try:
        local_date = date.fromisoformat((callback.data or "").split(":", 1)[1])
        data = await state.get_data()
        service_id = UUID(data["service_id"])
        duration_minutes = int(data["duration_minutes"])
    except (KeyError, ValueError):
        await callback.answer("Начните перенос заново", show_alert=True)
        return
    try:
        slots = await availability_service.list_slots(
            db_session,
            business_id=business_id,
            master_id=master.id,
            service_id=service_id,
            local_date=local_date,
            duration_minutes_override=duration_minutes,
        )
    except BookingConfigurationError:
        await callback.answer("Услуга или расписание больше недоступны", show_alert=True)
        return
    timezone = await _master_timezone(db_session, business_id, master)
    await state.update_data(local_date=local_date.isoformat())
    if not slots:
        await callback.answer("На эту дату свободных окон нет", show_alert=True)
        return
    await state.set_state(MasterStates.rescheduling_slot)
    await _edit(
        callback,
        f"Свободное время на {local_date:%d.%m.%Y}:",
        reply_markup=slots_keyboard(
            slots,
            timezone,
            callback_prefix="mrs",
            back_callback="master:move:dates",
        ),
    )
    await callback.answer()


@router.callback_query(MasterStates.rescheduling_slot, F.data.startswith("mrs:"))
async def master_select_reschedule_slot(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    data = await state.get_data()
    try:
        service_start = datetime.fromtimestamp(
            int((callback.data or "").split(":", 1)[1]),
            tz=UTC,
        )
        service_id = UUID(data["service_id"])
        holder_user_id = UUID(data["holder_user_id"])
        local_date = date.fromisoformat(data["local_date"])
        duration_minutes = int(data["duration_minutes"])
    except (KeyError, TypeError, ValueError):
        await callback.answer("Начните перенос заново", show_alert=True)
        return
    try:
        hold = await manual_booking_service.create_hold(
            db_session,
            business_id=business_id,
            master_id=master.id,
            service_id=service_id,
            client_id=holder_user_id,
            service_start=service_start,
            local_date=local_date,
            duration_minutes_override=duration_minutes,
        )
    except SlotUnavailableError:
        await callback.answer("Это время уже занято", show_alert=True)
        return
    summary = await manual_booking_service.get_hold_summary(
        db_session,
        hold_id=hold.id,
        client_id=holder_user_id,
    )
    await state.update_data(hold_id=str(hold.id))
    await state.set_state(MasterStates.rescheduling_confirming)
    await _edit(
        callback,
        "<b>Проверьте новое время</b>\n\n"
        f"Услуга: <b>{escape(summary.service_name)}</b>\n"
        f"Дата: <b>{summary.local_start:%d.%m.%Y}</b>\n"
        f"Время: <b>{summary.local_start:%H:%M}-{summary.local_end:%H:%M}</b>\n\n"
        "Старое время освободится только после подтверждения.",
        reply_markup=master_reschedule_confirmation_keyboard(),
    )
    await callback.answer()


@router.callback_query(
    MasterStates.rescheduling_confirming,
    F.data == "master:move:confirm",
)
async def master_confirm_reschedule(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    user, master = context
    data = await state.get_data()
    try:
        summary = await manual_booking_service.confirm_master_reschedule(
            db_session,
            business_id=business_id,
            master=master,
            appointment_id=UUID(data["appointment_id"]),
            hold_id=UUID(data["hold_id"]),
            holder_user_id=user.id,
        )
        appointment = await schedule_service.get_appointment(
            db_session,
            business_id=business_id,
            master=master,
            appointment_id=summary.appointment_id,
        )
    except (
        KeyError,
        ValueError,
        HoldNotFoundError,
        HoldExpiredError,
        AppointmentChangeNotAllowedError,
        ClientAppointmentNotFoundError,
        AppointmentNotFoundError,
    ):
        await state.clear()
        await callback.answer("Не удалось перенести запись", show_alert=True)
        return
    await state.clear()
    await _edit(
        callback,
        "Запись перенесена. Клиент получит уведомление.\n\n" + _format_appointment(appointment),
        reply_markup=master_appointment_actions_keyboard(appointment),
    )
    await callback.answer("Новое время сохранено")


@router.callback_query(F.data == "master:move:abort")
async def master_abort_reschedule(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    user, master = context
    data = await state.get_data()
    if "hold_id" in data:
        await manual_booking_service.release_hold(
            db_session,
            hold_id=UUID(data["hold_id"]),
            client_id=user.id,
        )
    try:
        appointment = await schedule_service.get_appointment(
            db_session,
            business_id=business_id,
            master=master,
            appointment_id=UUID(data["appointment_id"]),
        )
    except (KeyError, ValueError, AppointmentNotFoundError):
        await state.clear()
        await _edit(callback, "Прежнее время сохранено.", reply_markup=master_menu_keyboard())
    else:
        await state.clear()
        await _edit(
            callback,
            "Прежнее время сохранено.\n\n" + _format_appointment(appointment),
            reply_markup=master_appointment_actions_keyboard(appointment),
        )
    await callback.answer("Перенос отменён")


@router.callback_query(F.data.startswith("master:status:"))
async def master_status(
    callback: CallbackQuery,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    user, master = context
    try:
        _prefix, _status, appointment_raw, action = (callback.data or "").split(":")
        appointment_id = UUID(appointment_raw)
        new_status = STATUS_ACTIONS[action]
        appointment = await schedule_service.change_appointment_status(
            db_session,
            business_id=business_id,
            master=master,
            appointment_id=appointment_id,
            actor_user_id=user.id,
            new_status=new_status,
        )
    except (ValueError, KeyError, AppointmentNotFoundError):
        await callback.answer("Запись не найдена", show_alert=True)
        return
    except InvalidAppointmentTransitionError:
        await callback.answer("Статус записи уже изменился", show_alert=True)
        return
    await _edit(
        callback,
        _format_appointment(appointment),
        reply_markup=master_appointment_actions_keyboard(appointment),
    )
    await callback.answer("Статус обновлён")


@router.callback_query(F.data == "master:availability")
async def master_availability(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    if await _master_for_actor(callback.from_user, db_session, business_id) is None:
        await _deny(callback)
        return
    await state.clear()
    await _edit(
        callback,
        "<b>Управление графиком</b>\n\n"
        "Рабочие часы создают свободные окна. Выходные и блокировки исключают время.",
        reply_markup=master_availability_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "master:working-hours")
async def master_working_hours(
    callback: CallbackQuery,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    rules = await schedule_service.list_weekly_rules(
        db_session,
        business_id=business_id,
        master_id=master.id,
    )
    await _edit(
        callback,
        "<b>Рабочие часы</b>\n\nВыберите день недели для изменения:",
        reply_markup=master_weekdays_keyboard(rules),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("master:weekday:"))
async def master_select_weekday(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    if await _master_for_actor(callback.from_user, db_session, business_id) is None:
        await _deny(callback)
        return
    try:
        weekday = int((callback.data or "").rsplit(":", 1)[-1])
        if weekday not in range(7):
            raise ValueError
    except ValueError:
        await callback.answer("Некорректный день", show_alert=True)
        return
    await state.set_state(MasterStates.waiting_weekday_hours)
    await state.update_data(master_weekday=weekday)
    await _edit(
        callback,
        "Отправьте рабочие часы в формате <code>10:00-19:00</code>.\n"
        "Чтобы сделать день нерабочим, отправьте слово <code>выходной</code>.",
    )
    await callback.answer()


@router.message(MasterStates.waiting_weekday_hours, F.text)
async def master_receive_weekday_hours(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(message.from_user, db_session, business_id)
    if context is None:
        await state.clear()
        await message.answer("Кабинет специалиста недоступен.")
        return
    _user, master = context
    data = await state.get_data()
    weekday = int(data["master_weekday"])
    value = (message.text or "").strip().lower()
    if value == "выходной":
        start_time = end_time = None
    else:
        match = TIME_RANGE_PATTERN.fullmatch(value)
        if match is None:
            await message.answer("Используйте формат <code>10:00-19:00</code>.")
            return
        try:
            start_time = time.fromisoformat(match.group("start"))
            end_time = time.fromisoformat(match.group("end"))
            if end_time <= start_time:
                raise ValueError
        except ValueError:
            await message.answer("Проверьте время начала и окончания.")
            return
    await schedule_service.set_weekday_hours(
        db_session,
        business_id=business_id,
        master_id=master.id,
        weekday=weekday,
        start_time=start_time,
        end_time=end_time,
    )
    await state.clear()
    rules = await schedule_service.list_weekly_rules(
        db_session,
        business_id=business_id,
        master_id=master.id,
    )
    await message.answer(
        "Рабочие часы обновлены.",
        reply_markup=master_weekdays_keyboard(rules),
    )


async def _show_days_off(
    callback: CallbackQuery,
    session: AsyncSession,
    business_id: UUID,
    master: Master,
) -> None:
    timezone = await _master_timezone(session, business_id, master)
    today = datetime.now(UTC).astimezone(timezone).date()
    dates = [today + timedelta(days=offset) for offset in range(21)]
    selected = await schedule_service.list_day_off_dates(
        session,
        business_id=business_id,
        master_id=master.id,
        start_date=today,
        days=len(dates),
    )
    await _edit(
        callback,
        "<b>Выходные</b>\n\nНажмите дату, чтобы включить или убрать выходной:",
        reply_markup=master_days_off_keyboard(dates, selected),
    )


@router.callback_query(F.data == "master:days-off")
async def master_days_off(
    callback: CallbackQuery,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    await _show_days_off(callback, db_session, business_id, master)
    await callback.answer()


@router.callback_query(F.data.startswith("master:dayoff:"))
async def master_toggle_day_off(
    callback: CallbackQuery,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    try:
        local_date = date.fromisoformat((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Некорректная дата", show_alert=True)
        return
    enabled = await schedule_service.toggle_day_off(
        db_session,
        business_id=business_id,
        master_id=master.id,
        local_date=local_date,
    )
    await _show_days_off(callback, db_session, business_id, master)
    await callback.answer("Выходной добавлен" if enabled else "Выходной удалён")


@router.callback_query(F.data == "master:block:add")
async def master_add_block(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    if await _master_for_actor(callback.from_user, db_session, business_id) is None:
        await _deny(callback)
        return
    await state.set_state(MasterStates.waiting_time_block)
    await _edit(
        callback,
        "Отправьте интервал и необязательную причину:\n"
        "<code>25.07.2026 14:00-16:00 личные дела</code>",
    )
    await callback.answer()


@router.message(MasterStates.waiting_time_block, F.text)
async def master_receive_block(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(message.from_user, db_session, business_id)
    if context is None:
        await state.clear()
        await message.answer("Кабинет специалиста недоступен.")
        return
    _user, master = context
    match = DATED_RANGE_PATTERN.fullmatch((message.text or "").strip())
    if match is None:
        await message.answer("Используйте формат <code>25.07.2026 14:00-16:00 причина</code>.")
        return
    try:
        local_date = datetime.strptime(match.group("date"), "%d.%m.%Y").date()
        start_time = time.fromisoformat(match.group("start"))
        end_time = time.fromisoformat(match.group("end"))
        timezone = await _master_timezone(db_session, business_id, master)
        starts_at = datetime.combine(local_date, start_time, timezone)
        ends_at = datetime.combine(local_date, end_time, timezone)
        await schedule_service.create_time_block(
            db_session,
            business_id=business_id,
            master_id=master.id,
            starts_at=starts_at,
            ends_at=ends_at,
            reason=match.group("reason"),
        )
    except ValueError:
        await message.answer("Проверьте дату и время интервала.")
        return
    except ScheduleConflictError:
        await message.answer("Это время пересекается с записью или другой блокировкой.")
        return
    await state.clear()
    await message.answer(
        "Время заблокировано.",
        reply_markup=master_availability_keyboard(),
    )


@router.callback_query(F.data == "master:blocks")
async def master_blocks(
    callback: CallbackQuery,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    blocks = await schedule_service.list_time_blocks(
        db_session,
        business_id=business_id,
        master=master,
    )
    text = (
        "<b>Активные блокировки</b>\n\nНажмите блокировку, чтобы удалить."
        if blocks
        else "<b>Активные блокировки</b>\n\nБлокировок нет."
    )
    await _edit(callback, text, reply_markup=master_blocks_keyboard(blocks))
    await callback.answer()


@router.callback_query(F.data.startswith("master:block:remove:"))
async def master_remove_block(
    callback: CallbackQuery,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    _user, master = context
    try:
        block_id = UUID((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Некорректная блокировка", show_alert=True)
        return
    await schedule_service.release_time_block(
        db_session,
        business_id=business_id,
        master_id=master.id,
        block_id=block_id,
    )
    blocks = await schedule_service.list_time_blocks(
        db_session,
        business_id=business_id,
        master=master,
    )
    await _edit(
        callback,
        "<b>Активные блокировки</b>",
        reply_markup=master_blocks_keyboard(blocks),
    )
    await callback.answer("Блокировка удалена")


@router.callback_query(F.data == "master:extra-day")
async def master_extra_day(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    if await _master_for_actor(callback.from_user, db_session, business_id) is None:
        await _deny(callback)
        return
    await state.set_state(MasterStates.waiting_extra_day)
    await _edit(
        callback,
        "Отправьте дату и рабочие часы дополнительного дня:\n<code>26.07.2026 11:00-17:00</code>",
    )
    await callback.answer()


@router.message(MasterStates.waiting_extra_day, F.text)
async def master_receive_extra_day(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(message.from_user, db_session, business_id)
    if context is None:
        await state.clear()
        await message.answer("Кабинет специалиста недоступен.")
        return
    _user, master = context
    match = DATED_RANGE_PATTERN.fullmatch((message.text or "").strip())
    if match is None:
        await message.answer("Используйте формат <code>26.07.2026 11:00-17:00</code>.")
        return
    try:
        local_date = datetime.strptime(match.group("date"), "%d.%m.%Y").date()
        start_time = time.fromisoformat(match.group("start"))
        end_time = time.fromisoformat(match.group("end"))
        await schedule_service.add_extra_day(
            db_session,
            business_id=business_id,
            master_id=master.id,
            local_date=local_date,
            start_time=start_time,
            end_time=end_time,
        )
    except ValueError:
        await message.answer("Проверьте дату и рабочие часы.")
        return
    await state.clear()
    await message.answer(
        "Дополнительный рабочий день добавлен.",
        reply_markup=master_availability_keyboard(),
    )


@router.callback_query(F.data == "master:notifications")
async def master_notifications(
    callback: CallbackQuery,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    user, master = context
    enabled = await master_notifications_enabled(
        db_session,
        business_id=business_id,
        user_id=user.id,
    )
    reminders = await get_client_reminder_settings(
        db_session,
        business_id=business_id,
        master_user_id=user.id,
    )
    await _edit(
        callback,
        "<b>Уведомления</b>\n\n"
        "Настройте сообщения специалисту и напоминания клиентам. "
        "Изменения применяются и к будущим уже созданным записям.",
        reply_markup=master_notifications_keyboard(enabled, reminders),
    )
    await callback.answer()


@router.callback_query(F.data == "master:notifications:toggle")
async def master_toggle_notifications(
    callback: CallbackQuery,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    user, _master = context
    enabled = await toggle_master_notifications(
        db_session,
        business_id=business_id,
        user_id=user.id,
    )
    reminders = await get_client_reminder_settings(
        db_session,
        business_id=business_id,
        master_user_id=user.id,
    )
    await _edit(
        callback,
        "<b>Уведомления</b>\n\n"
        "Настройте сообщения специалисту и напоминания клиентам. "
        "Изменения применяются и к будущим уже созданным записям.",
        reply_markup=master_notifications_keyboard(enabled, reminders),
    )
    await callback.answer("Уведомления включены" if enabled else "Уведомления выключены")


@router.callback_query(F.data.startswith("master:reminders:toggle:"))
async def master_toggle_client_reminder(
    callback: CallbackQuery,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(callback.from_user, db_session, business_id)
    if context is None:
        await _deny(callback)
        return
    user, master = context
    toggle = (callback.data or "").rsplit(":", 1)[-1]
    try:
        reminders = await update_client_reminder_settings(
            db_session,
            business_id=business_id,
            master_user_id=user.id,
            toggle=toggle,
        )
    except ValueError:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    await manual_booking_service.rebuild_client_reminders_for_master(
        db_session,
        business_id=business_id,
        master=master,
    )
    enabled = await master_notifications_enabled(
        db_session,
        business_id=business_id,
        user_id=user.id,
    )
    await _edit(
        callback,
        "<b>Уведомления</b>\n\n"
        "Настройте сообщения специалисту и напоминания клиентам. "
        "Изменения применяются и к будущим уже созданным записям.",
        reply_markup=master_notifications_keyboard(enabled, reminders),
    )
    await callback.answer("Настройки напоминаний сохранены")


@router.callback_query(F.data == "master:reminders:hour")
async def master_set_reminder_hour(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    if await _master_for_actor(callback.from_user, db_session, business_id) is None:
        await _deny(callback)
        return
    await state.set_state(MasterStates.waiting_reminder_hour)
    await _edit(
        callback,
        "Отправьте час утреннего напоминания клиенту — целое число от "
        "<b>0</b> до <b>23</b>.\n\nНапример: <code>9</code>.",
    )
    await callback.answer()


@router.message(MasterStates.waiting_reminder_hour, F.text)
async def master_receive_reminder_hour(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    business_id: UUID,
) -> None:
    context = await _master_for_actor(message.from_user, db_session, business_id)
    if context is None:
        await state.clear()
        await message.answer("Кабинет специалиста недоступен.")
        return
    user, master = context
    try:
        hour = int((message.text or "").strip())
        reminders = await update_client_reminder_settings(
            db_session,
            business_id=business_id,
            master_user_id=user.id,
            day_of_hour=hour,
        )
    except ValueError:
        await message.answer("Введите целое число от 0 до 23.")
        return
    await manual_booking_service.rebuild_client_reminders_for_master(
        db_session,
        business_id=business_id,
        master=master,
    )
    enabled = await master_notifications_enabled(
        db_session,
        business_id=business_id,
        user_id=user.id,
    )
    await state.clear()
    await message.answer(
        f"Время утреннего напоминания изменено на <b>{hour:02d}:00</b>.",
        reply_markup=master_notifications_keyboard(enabled, reminders),
    )

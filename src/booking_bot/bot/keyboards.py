from datetime import date, datetime
from uuid import UUID

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from booking_bot.db.models import Service
from booking_bot.domain.enums import AppointmentStatus
from booking_bot.domain.money import format_money
from booking_bot.services.availability import BookableSlot
from booking_bot.services.bookings import AppointmentSummary
from booking_bot.services.master_schedule import (
    MasterAppointment,
    MasterTimeBlock,
    WeeklyWorkingInterval,
)
from booking_bot.services.reminder_settings import ClientReminderSettings
from booking_bot.specialist_config import get_specialist_template

WEEKDAYS_RU = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def main_menu_keyboard(
    *, master_access: bool = False, unread_count: int = 0
) -> InlineKeyboardMarkup:
    template = get_specialist_template()
    rows = [
        [
            InlineKeyboardButton(
                text=template.button("book", "Записаться"),
                callback_data="booking:start",
            )
        ],
        [
            InlineKeyboardButton(
                text=template.button("my_bookings", "Мои записи"),
                callback_data="booking:mine",
            )
        ],
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="Мои заявки" + (f" 🔴 {unread_count}" if unread_count else ""),
                callback_data="rq:mine",
            )
        ]
    )
    if master_access:
        rows.append(
            [
                InlineKeyboardButton(
                    text=template.button("specialist_cabinet", "Мой кабинет"),
                    callback_data="master:menu",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=template.button("help", "Помощь"),
                callback_data="help",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def services_keyboard(services: list[Service]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for service in services:
        price = (
            f" - {'от ' if service.pricing_mode == 'from' else ''}"
            f"{format_money(service.price_minor, service.currency)}"
            if service.price_minor is not None
            else ""
        )
        if service.pricing_mode == "negotiable":
            price = " — стоимость после обсуждения"
        builder.button(text=f"{service.name}{price}", callback_data=f"service:{service.id}")
    builder.button(
        text=get_specialist_template().button("home", "В главное меню"),
        callback_data="menu:home",
    )
    builder.adjust(1)
    return builder.as_markup()


def dates_keyboard(
    dates: list[date],
    *,
    callback_prefix: str = "date",
    back_callback: str = "booking:start",
    back_text: str | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item in dates:
        builder.button(
            text=f"{WEEKDAYS_RU[item.weekday()]}, {item:%d.%m}",
            callback_data=f"{callback_prefix}:{item.isoformat()}",
        )
    builder.button(
        text=back_text or get_specialist_template().button("back_to_services", "Назад к услугам"),
        callback_data=back_callback,
    )
    builder.button(
        text=get_specialist_template().button("home", "В главное меню"),
        callback_data="menu:home",
    )
    builder.adjust(2)
    return builder.as_markup()


def slots_keyboard(
    slots: list[BookableSlot],
    timezone,
    *,
    callback_prefix: str = "slot",
    back_callback: str = "booking:back:dates",
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for slot in slots:
        local_start = slot.service_start.astimezone(timezone)
        builder.button(
            text=local_start.strftime("%H:%M"),
            callback_data=f"{callback_prefix}:{int(slot.service_start.timestamp())}",
        )
    template = get_specialist_template()
    builder.button(
        text=template.button("back_to_dates", "Назад к датам"),
        callback_data=back_callback,
    )
    builder.button(
        text=template.button("home", "В главное меню"),
        callback_data="menu:home",
    )
    builder.adjust(3)
    return builder.as_markup()


def confirmation_keyboard() -> InlineKeyboardMarkup:
    template = get_specialist_template()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=template.button("confirm_booking", "Подтвердить запись"),
                    callback_data="booking:confirm",
                )
            ],
            [
                InlineKeyboardButton(
                    text=template.button("cancel", "Отменить"),
                    callback_data="booking:abort",
                )
            ],
        ]
    )


def client_appointments_keyboard(
    appointments: list[AppointmentSummary],
    *,
    back_callback: str = "booking:mine",
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item in appointments:
        builder.button(
            text=f"{item.local_start:%d.%m %H:%M} · {item.service_name}",
            callback_data=f"appt:v:{item.appointment_id}",
        )
    builder.button(text="К разделам записей", callback_data=back_callback)
    builder.adjust(1)
    return builder.as_markup()


def client_booking_sections_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Предстоящие",
                    callback_data="booking:list:upcoming",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Прошлые",
                    callback_data="booking:list:past",
                ),
                InlineKeyboardButton(
                    text="Отменённые",
                    callback_data="booking:list:cancelled",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=get_specialist_template().button("home", "В главное меню"),
                    callback_data="menu:home",
                )
            ],
        ]
    )


def client_appointment_actions_keyboard(
    appointment: AppointmentSummary,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if appointment.booking_request_id:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Обсуждение с мастером",
                    callback_data=f"rq:appointment:{appointment.appointment_id.hex}",
                )
            ]
        )
    if appointment.can_change:
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="Перенести запись",
                        callback_data=f"appt:r:{appointment.appointment_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Отменить запись",
                        callback_data=f"appt:c:{appointment.appointment_id}",
                    )
                ],
            ]
        )
    repeatable = appointment.service_id is not None and (
        appointment.local_end <= datetime.now(appointment.local_end.tzinfo)
        or appointment.status
        in {
            AppointmentStatus.COMPLETED.value,
            AppointmentStatus.NO_SHOW.value,
            AppointmentStatus.CANCELLED_BY_CLIENT.value,
            AppointmentStatus.CANCELLED_BY_MASTER.value,
        }
    )
    if repeatable:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Записаться повторно",
                    callback_data=f"appt:repeat:{appointment.appointment_id}",
                )
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="Добавить в календарь",
                    callback_data=f"appt:ics:{appointment.appointment_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Связаться со специалистом",
                    callback_data=f"appt:contact:{appointment.appointment_id}",
                )
            ],
            [InlineKeyboardButton(text="К моим записям", callback_data="booking:mine")],
            [
                InlineKeyboardButton(
                    text=get_specialist_template().button("home", "В главное меню"),
                    callback_data="menu:home",
                )
            ],
        ]
    )
    if appointment.booking_request_id:
        rows = [
            row
            for row in rows
            if not any(button.callback_data.startswith("appt:contact:") for button in row)
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def client_cancel_confirmation_keyboard(appointment_id: UUID) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, отменить",
                    callback_data=f"appt:cy:{appointment_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Нет, оставить запись",
                    callback_data=f"appt:v:{appointment_id}",
                )
            ],
        ]
    )


def reschedule_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подтвердить перенос",
                    callback_data="appt:rc",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Оставить прежнее время",
                    callback_data="appt:ra",
                )
            ],
        ]
    )


def phone_keyboard() -> ReplyKeyboardMarkup:
    template = get_specialist_template()
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text=template.button("share_phone", "Отправить мой телефон"),
                    request_contact=True,
                )
            ],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder=template.button(
            "phone_placeholder",
            "Нажмите кнопку или введите номер",
        ),
    )


def master_menu_keyboard(*, unread_count: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Заявки и диалоги" + (f" 🔴 {unread_count}" if unread_count else ""),
                    callback_data="rq:inbox",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Сегодня",
                    callback_data="master:schedule:today",
                ),
                InlineKeyboardButton(
                    text="Завтра",
                    callback_data="master:schedule:tomorrow",
                ),
            ],
            [
                InlineKeyboardButton(text="Неделя", callback_data="master:schedule:week"),
                InlineKeyboardButton(text="30 дней", callback_data="master:month:page:0"),
            ],
            [
                InlineKeyboardButton(
                    text="Выбрать дату",
                    callback_data="master:schedule:choose",
                )
            ],
            [
                InlineKeyboardButton(
                    text="➕ Добавить запись",
                    callback_data="mb:start",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Статистика",
                    callback_data="master:analytics:current_month",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Управление графиком",
                    callback_data="master:availability",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Услуги и цены",
                    callback_data="master:services",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Уведомления",
                    callback_data="master:notifications",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚙️ Профиль и расписание",
                    callback_data="setup:profile:start",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Клиентское меню",
                    callback_data="master:client-menu",
                )
            ],
        ]
    )


def master_analytics_keyboard(selected_period: str) -> InlineKeyboardMarkup:
    options = [
        ("current_month", "Текущий месяц"),
        ("previous_month", "Прошлый месяц"),
        ("7_days", "7 дней"),
        ("30_days", "30 дней"),
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(options), 2):
        row = []
        for key, label in options[index : index + 2]:
            marker = "• " if key == selected_period else ""
            row.append(
                InlineKeyboardButton(
                    text=f"{marker}{label}",
                    callback_data=f"master:analytics:{key}",
                )
            )
        rows.append(row)
    rows.append([InlineKeyboardButton(text="Назад", callback_data="master:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def master_manual_services_keyboard(services: list[Service]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for service in services:
        builder.button(
            text=f"{service.name} · {_service_price_label(service)}",
            callback_data=f"mb:s:{service.id}",
        )
    builder.button(text="Отмена", callback_data="mb:abort")
    builder.adjust(1)
    return builder.as_markup()


def master_manual_dates_keyboard(dates: list[date]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item in dates:
        builder.button(
            text=f"{WEEKDAYS_RU[item.weekday()]}, {item:%d.%m}",
            callback_data=f"mb:d:{item.isoformat()}",
        )
    builder.button(text="Назад к услугам", callback_data="mb:start")
    builder.button(text="Отмена", callback_data="mb:abort")
    builder.adjust(2)
    return builder.as_markup()


def master_manual_slots_keyboard(
    slots: list[BookableSlot],
    timezone,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for slot in slots:
        builder.button(
            text=slot.service_start.astimezone(timezone).strftime("%H:%M"),
            callback_data=f"mb:t:{int(slot.service_start.timestamp())}",
        )
    builder.button(text="Назад к датам", callback_data="mb:dates")
    builder.button(text="Отмена", callback_data="mb:abort")
    builder.adjust(3)
    return builder.as_markup()


def master_manual_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="mb:abort")],
        ]
    )


def master_manual_comment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Без комментария", callback_data="mb:comment:skip")],
            [InlineKeyboardButton(text="Отмена", callback_data="mb:abort")],
        ]
    )


def master_manual_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Создать запись", callback_data="mb:confirm")],
            [InlineKeyboardButton(text="Отмена", callback_data="mb:abort")],
        ]
    )


def _service_price_label(service: Service) -> str:
    if service.price_minor is None:
        return "цена не указана"
    return format_money(service.price_minor, service.currency)


def master_services_keyboard(services: list[Service]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for service in services:
        status = "✅" if service.is_active else "⏸"
        builder.button(
            text=f"{status} {service.name}",
            callback_data=f"svc:v:{service.id}",
        )
    builder.button(text="➕ Добавить услугу", callback_data="svc:new")
    builder.button(
        text=get_specialist_template().button("specialist_cabinet", "Мой кабинет"),
        callback_data="master:menu",
    )
    builder.adjust(1)
    return builder.as_markup()


def master_service_actions_keyboard(service: Service) -> InlineKeyboardMarkup:
    approval = "✅ Ручное подтверждение" if service.requires_approval else "Автоподтверждение"
    visibility = "Скрыть услугу" if service.is_active else "Опубликовать услугу"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Название",
                    callback_data=f"svc:e:n:{service.id}",
                ),
                InlineKeyboardButton(
                    text="Описание",
                    callback_data=f"svc:e:d:{service.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Длительность",
                    callback_data=f"svc:e:t:{service.id}",
                ),
                InlineKeyboardButton(
                    text="Цена",
                    callback_data=f"svc:e:p:{service.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Перерывы",
                    callback_data=f"svc:e:b:{service.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=approval,
                    callback_data=f"svc:t:r:{service.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=visibility,
                    callback_data=f"svc:t:a:{service.id}",
                )
            ],
            [InlineKeyboardButton(text="К списку услуг", callback_data="master:services")],
        ]
    )


def master_service_cancel_keyboard(service_id: UUID | None = None) -> InlineKeyboardMarkup:
    callback_data = f"svc:v:{service_id}" if service_id is not None else "master:services"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data=callback_data)],
        ]
    )


def master_appointments_keyboard(
    appointments: list[MasterAppointment],
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item in appointments:
        builder.button(
            text=f"{item.local_start:%d.%m %H:%M} · {item.service_name}",
            callback_data=f"master:appointment:{item.appointment_id}",
        )
    builder.button(
        text=get_specialist_template().button("specialist_cabinet", "Мой кабинет"),
        callback_data="master:menu",
    )
    builder.adjust(1)
    return builder.as_markup()


def master_month_schedule_keyboard(
    *,
    page: int,
    page_count: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="← Назад",
                callback_data=f"master:month:page:{page - 1}",
            )
        )
    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton(
                text="Вперёд →",
                callback_data=f"master:month:page:{page + 1}",
            )
        )
    if navigation:
        rows.append(navigation)
    rows.append(
        [
            InlineKeyboardButton(
                text="📥 Скачать Excel",
                callback_data="master:month:export",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=get_specialist_template().button("specialist_cabinet", "Мой кабинет"),
                callback_data="master:menu",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def master_schedule_dates_keyboard(dates: list[date]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item in dates:
        builder.button(
            text=f"{WEEKDAYS_RU[item.weekday()]}, {item:%d.%m}",
            callback_data=f"master:schedule:date:{item.isoformat()}",
        )
    builder.button(text="Назад", callback_data="master:menu")
    builder.adjust(2)
    return builder.as_markup()


def master_appointment_actions_keyboard(
    appointment: MasterAppointment,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if appointment.booking_request_id:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Диалог с клиентом",
                    callback_data=f"rq:appointment:{appointment.appointment_id.hex}",
                )
            ]
        )
    if appointment.status == AppointmentStatus.PENDING_APPROVAL.value:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Подтвердить",
                    callback_data=f"master:status:{appointment.appointment_id}:approve",
                )
            ]
        )
    if (
        appointment.status == AppointmentStatus.CONFIRMED.value
        and appointment.local_start <= datetime.now(appointment.local_start.tzinfo)
    ):
        rows.append(
            [
                InlineKeyboardButton(
                    text="Выполнено",
                    callback_data=f"master:status:{appointment.appointment_id}:complete",
                ),
                InlineKeyboardButton(
                    text="Не пришёл",
                    callback_data=f"master:status:{appointment.appointment_id}:noshow",
                ),
            ]
        )
    if appointment.status in {
        AppointmentStatus.PENDING_APPROVAL.value,
        AppointmentStatus.CONFIRMED.value,
    }:
        if appointment.local_start > datetime.now(appointment.local_start.tzinfo):
            rows.append(
                [
                    InlineKeyboardButton(
                        text="Перенести",
                        callback_data=f"master:move-start:{appointment.appointment_id}",
                    ),
                    InlineKeyboardButton(
                        text="Изменить длительность",
                        callback_data=f"master:duration:{appointment.appointment_id}",
                    ),
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Отменить запись",
                    callback_data=f"master:status:{appointment.appointment_id}:cancel",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="Внутренняя заметка",
                callback_data=f"master:note:{appointment.appointment_id}",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="К расписанию", callback_data="master:schedule:today")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def master_reschedule_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подтвердить перенос",
                    callback_data="master:reschedule:confirm",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Оставить прежнее время",
                    callback_data="master:reschedule:abort",
                )
            ],
        ]
    )


def master_availability_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Рабочие часы",
                    callback_data="master:working-hours",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Перенастроить всю неделю",
                    callback_data="setup:schedule:start",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Выходные",
                    callback_data="master:days-off",
                ),
                InlineKeyboardButton(
                    text="Доп. рабочий день",
                    callback_data="master:extra-day",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Заблокировать время",
                    callback_data="master:block:add",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Активные блокировки",
                    callback_data="master:blocks",
                )
            ],
            [InlineKeyboardButton(text="Назад", callback_data="master:menu")],
        ]
    )


def specialist_setup_bio_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оставить текущее", callback_data="setup:bio:skip")],
            [InlineKeyboardButton(text="Отмена", callback_data="setup:cancel")],
        ]
    )


def specialist_setup_days_keyboard(selected: set[int]) -> InlineKeyboardMarkup:
    day_buttons = [
        InlineKeyboardButton(
            text=f"{'✅ ' if weekday in selected else ''}{name}",
            callback_data=f"setup:day:{weekday}",
        )
        for weekday, name in enumerate(WEEKDAYS_RU)
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            day_buttons[0:2],
            day_buttons[2:4],
            day_buttons[4:6],
            day_buttons[6:7],
            [
                InlineKeyboardButton(text="Пн–Пт", callback_data="setup:preset:weekdays"),
                InlineKeyboardButton(text="Каждый день", callback_data="setup:preset:all"),
            ],
            [InlineKeyboardButton(text="Очистить", callback_data="setup:preset:clear")],
            [InlineKeyboardButton(text="Продолжить", callback_data="setup:days:done")],
            [InlineKeyboardButton(text="Отмена", callback_data="setup:cancel")],
        ]
    )


def specialist_setup_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="setup:cancel")],
        ]
    )


def specialist_setup_hours_keyboard(
    source_weekday: int | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if source_weekday is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Сохранить время для каждого рабочего дня",
                    callback_data=f"setup:hours:all:{source_weekday}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="setup:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def specialist_setup_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Сохранить", callback_data="setup:save")],
            [InlineKeyboardButton(text="Изменить дни", callback_data="setup:days:change")],
            [InlineKeyboardButton(text="Отмена", callback_data="setup:cancel")],
        ]
    )


def master_weekdays_keyboard(
    rules: list[WeeklyWorkingInterval],
) -> InlineKeyboardMarkup:
    intervals: dict[int, list[str]] = {}
    for rule in rules:
        intervals.setdefault(rule.weekday, []).append(
            f"{rule.start_time:%H:%M}-{rule.end_time:%H:%M}"
        )
    builder = InlineKeyboardBuilder()
    for weekday, name in enumerate(WEEKDAYS_RU):
        hours = ", ".join(intervals.get(weekday, [])) or "выходной"
        builder.button(
            text=f"{name}: {hours}",
            callback_data=f"master:weekday:{weekday}",
        )
    builder.button(text="Назад", callback_data="master:availability")
    builder.adjust(1)
    return builder.as_markup()


def master_days_off_keyboard(
    dates: list[date],
    selected: set[date],
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item in dates:
        marker = "✅ " if item in selected else ""
        builder.button(
            text=f"{marker}{WEEKDAYS_RU[item.weekday()]}, {item:%d.%m}",
            callback_data=f"master:dayoff:{item.isoformat()}",
        )
    builder.button(text="Назад", callback_data="master:availability")
    builder.adjust(2)
    return builder.as_markup()


def master_blocks_keyboard(blocks: list[MasterTimeBlock]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for block in blocks:
        builder.button(
            text=f"Удалить {block.local_start:%d.%m %H:%M}-{block.local_end:%H:%M}",
            callback_data=f"master:block:remove:{block.block_id}",
        )
    builder.button(text="Назад", callback_data="master:availability")
    builder.adjust(1)
    return builder.as_markup()


def master_notifications_keyboard(
    enabled: bool,
    reminders: ClientReminderSettings,
) -> InlineKeyboardMarkup:
    state = "включены" if enabled else "выключены"
    seven_days = "вкл." if reminders.seven_days else "выкл."
    three_days = "вкл." if reminders.three_days else "выкл."
    day_of = "вкл." if reminders.day_of else "выкл."
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Новые записи: {state}",
                    callback_data="master:notifications:toggle",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"За 7 дней: {seven_days}",
                    callback_data="master:reminders:toggle:7d",
                ),
                InlineKeyboardButton(
                    text=f"За 3 дня: {three_days}",
                    callback_data="master:reminders:toggle:3d",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"В день записи: {day_of}",
                    callback_data="master:reminders:toggle:day",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"Время утреннего напоминания: {reminders.day_of_hour:02d}:00",
                    callback_data="master:reminders:hour",
                )
            ],
            [InlineKeyboardButton(text="Назад", callback_data="master:menu")],
        ]
    )

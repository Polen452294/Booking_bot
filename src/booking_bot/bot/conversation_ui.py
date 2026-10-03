"""Compact callbacks, navigation and Telegram-safe conversation rendering."""

from datetime import datetime
from html import escape, unescape
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from booking_bot.domain.money import format_money

STATUS_LABELS = {
    "draft": "Черновик",
    "waiting_master": "Ждём ответа мастера",
    "waiting_client": "Ждём клиента",
    "terms_proposed": "Предложена стоимость",
    "terms_accepted": "Условия согласованы",
    "booked": "Запись создана",
    "cancelled": "Заявка отменена",
    "closed": "Заявка закрыта",
}
EVENT_LABELS = {
    "request_created": "Заявка создана",
    "request_submitted": "Заявка отправлена",
    "request_cancelled": "Заявка отменена",
    "conversation_closed": "Диалог закрыт",
    "price_accepted": "✅ Стоимость согласована",
    "price_rejected": "Предложение отклонено",
    "price_superseded": "Прежнее предложение заменено",
    "appointment_created": "✅ Запись создана",
}


def cb(action: str, identifier: UUID, extra: int | None = None) -> str:
    value = f"rq:{action}:{identifier.hex}"
    return f"{value}:{extra}" if extra is not None else value


def keyboard(*buttons: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data)] for text, data in buttons
        ]
    )


def request_link(request_id: UUID, *, dialog: bool = False) -> InlineKeyboardMarkup:
    return keyboard(
        (
            "Открыть диалог" if dialog else "Открыть заявку",
            cb("chat" if dialog else "view", request_id),
        )
    )


async def edit_or_answer(callback: CallbackQuery, text: str, **kwargs) -> None:
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, **kwargs)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                await callback.message.answer(text, **kwargs)


def body(message, actor_id: UUID, timezone: str = "Europe/Moscow") -> str:
    if message.message_type == "system":
        payload = message.event_payload or {}
        label = EVENT_LABELS.get(message.event_type, "Событие заявки")
        if message.event_type == "price_proposed":
            price = format_money(payload["amount_minor"], payload["currency"])
            previous = payload.get("previous_amount_minor")
            label = (
                f"💰 Мастер изменил предложение: "
                f"{format_money(previous, payload['currency'])} → {price}"
                if previous is not None
                else f"💰 Мастер предложил стоимость: {price}"
            )
        elif message.event_type == "appointment_created":
            if payload.get("starts_at"):
                starts = datetime.fromisoformat(payload["starts_at"]).astimezone(ZoneInfo(timezone))
                label += f"\n{starts:%d.%m.%Y %H:%M}"
        if message.event_type in {"price_proposed", "price_accepted", "appointment_created"}:
            if "amount_minor" in payload and message.event_type != "price_proposed":
                label += f"\n{format_money(payload['amount_minor'], payload['currency'])}"
            if payload.get("comment"):
                label += f"\n{payload['comment']}"
        return f"────────────\n{escape(label)}\n────────────"
    author = (
        "Вы"
        if message.sender_user_id == actor_id
        else "Мастер"
        if message.sender_role == "master"
        else "Клиент"
    )
    return f"<b>{author}:</b>\n{escape(message.text or '')}"


async def render_history(
    target: Message,
    messages: list,
    actor_id: UUID,
    timezone: str = "Europe/Moscow",
) -> None:
    # Contiguous photos are rendered using Telegram's native album, max 10 items.
    index = 0
    pending = []
    pending_length = 0

    async def flush_text():
        if pending:
            await target.answer("\n\n".join(pending))
            pending.clear()

    while index < len(messages):
        item = messages[index]
        if item.message_type == "photo":
            await flush_text()
            pending_length = 0
            photos = [item]
            while (
                index + len(photos) < len(messages)
                and len(photos) < 10
                and messages[index + len(photos)].message_type == "photo"
                and messages[index + len(photos)].sender_user_id == item.sender_user_id
            ):
                photos.append(messages[index + len(photos)])
            # Captions are limited to 1024 by Telegram; long bodies get a separate message.
            media = []
            for photo in photos:
                caption = body(photo, actor_id)
                if len(photo.text or "") > 900:
                    await target.answer(caption)
                    caption = None
                media.append(InputMediaPhoto(media=photo.telegram_file_id, caption=caption))
            if len(media) > 1:
                await target.answer_media_group(media)
            else:
                await target.answer_photo(media[0].media, caption=media[0].caption)
            index += len(photos)
            continue
        text = body(item, actor_id, timezone)
        if item.message_type == "document":
            await flush_text()
            pending_length = 0
            if len(item.text or "") > 900:
                await target.answer(text)
                text = None
            await target.answer_document(item.telegram_file_id, caption=text)
        else:
            # Batch small messages to reduce Telegram flood pressure. Count decoded
            # text conservatively (including HTML tags), never split/truncate a message.
            length = len(unescape(text))
            if pending and pending_length + 2 + length > 4096:
                await flush_text()
                pending_length = 0
            pending_length += length + (2 if pending else 0)
            pending.append(text)
        index += 1
    await flush_text()

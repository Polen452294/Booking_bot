import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from booking_bot.config import Settings
from booking_bot.db.models import (
    Appointment,
    Business,
    CalendarEntry,
    Location,
    Master,
    NotificationJob,
    NotificationPreference,
    TelegramUser,
)
from booking_bot.db.session import async_session_factory
from booking_bot.domain.conversations import ConversationAccessError
from booking_bot.domain.enums import NotificationJobState
from booking_bot.domain.money import format_money
from booking_bot.services.conversation_context import REQUEST_NOTIFICATION_TITLES, get_request
from booking_bot.services.price_proposals import PriceProposalService
from booking_bot.services.reminder_settings import get_client_reminder_settings
from booking_bot.services.worker_health import HEARTBEAT_INTERVAL, WorkerHeartbeat
from booking_bot.specialist_config import get_specialist_template

logger = logging.getLogger(__name__)
DELIVERY_TIMEOUT = 60


class NotificationDeliveryError(RuntimeError):
    pass


class UnsupportedNotificationError(NotificationDeliveryError):
    pass


MASTER_NOTIFICATION_KINDS = {
    "master_new_appointment",
    "master_appointment_cancelled_by_client",
    "master_appointment_rescheduled_by_client",
    "master_new_booking_request",
    "master_new_conversation_message",
    "master_price_proposal_accepted",
    "master_price_proposal_rejected",
    "master_booking_request_cancelled",
}
CLIENT_REMINDER_KINDS = {"client_reminder_7d", "client_reminder_3d", "client_reminder_day_of"}


@dataclass(frozen=True, slots=True)
class DeliveryPayload:
    chat_id: int
    text: str
    reply_markup: InlineKeyboardMarkup | None = None


async def master_notifications_enabled(
    session: AsyncSession,
    *,
    business_id: UUID,
    user_id: UUID,
) -> bool:
    preference = await session.scalar(
        select(NotificationPreference).where(
            NotificationPreference.business_id == business_id,
            NotificationPreference.user_id == user_id,
        )
    )
    if preference is None:
        return True
    return bool(preference.settings.get("master_new_appointment", True))


async def toggle_master_notifications(
    session: AsyncSession,
    *,
    business_id: UUID,
    user_id: UUID,
) -> bool:
    preference = await session.scalar(
        select(NotificationPreference).where(
            NotificationPreference.business_id == business_id,
            NotificationPreference.user_id == user_id,
        )
    )
    if preference is None:
        preference = NotificationPreference(
            business_id=business_id,
            user_id=user_id,
            settings={"master_new_appointment": False},
        )
        session.add(preference)
        await session.flush()
        return False
    settings = dict(preference.settings)
    enabled = not bool(settings.get("master_new_appointment", True))
    settings["master_new_appointment"] = enabled
    preference.settings = settings
    await session.flush()
    return enabled


class NotificationDeliveryService:
    def __init__(self, settings: Settings, heartbeat: WorkerHeartbeat | None = None) -> None:
        self._settings = settings
        self._claim_token = uuid4()
        self._heartbeat = heartbeat

    async def _beat(self) -> None:
        if self._heartbeat is not None:
            await self._heartbeat.beat()

    async def run_once(
        self,
        bot: Bot,
        *,
        business_id: UUID,
        now: datetime | None = None,
        stop: asyncio.Event | None = None,
    ) -> int:
        now = now or datetime.now(UTC)
        if stop is not None and stop.is_set():
            return 0
        await self._beat()
        job_ids = await self._claim_jobs(business_id=business_id, now=now)
        for index, job_id in enumerate(job_ids):
            if stop is not None and stop.is_set():
                await self._release_jobs(job_ids[index:])
                return index
            try:
                async with asyncio.timeout(DELIVERY_TIMEOUT):
                    await self._deliver_job(bot, job_id=job_id, now=now)
                await self._beat()
            except (Exception, asyncio.CancelledError):
                # The current send may have reached Telegram. Leave its lease for recovery;
                # only unstarted jobs can safely be returned without consuming an attempt.
                await self._release_jobs(job_ids[index + 1 :])
                raise
        return len(job_ids)

    async def run_forever(
        self,
        bot: Bot,
        *,
        business_id: UUID,
        stop: asyncio.Event | None = None,
    ) -> None:
        stop = stop if stop is not None else asyncio.Event()
        while not stop.is_set():
            processed = await self.run_once(bot, business_id=business_id, stop=stop)
            await self._beat()
            if processed == 0:
                try:
                    async with asyncio.timeout(
                        min(self._settings.notification_poll_interval_seconds, HEARTBEAT_INTERVAL)
                    ):
                        await stop.wait()
                except TimeoutError:
                    pass

    async def _release_jobs(self, job_ids: list[UUID]) -> None:
        if not job_ids:
            return
        async with async_session_factory() as session:
            await session.execute(
                update(NotificationJob)
                .where(
                    NotificationJob.id.in_(job_ids),
                    NotificationJob.state == NotificationJobState.PROCESSING.value,
                    NotificationJob.claim_token == self._claim_token,
                )
                .values(
                    state=NotificationJobState.PENDING.value,
                    attempt_count=NotificationJob.attempt_count - 1,
                    updated_at=datetime.now(UTC),
                    claim_token=None,
                )
            )
            await session.commit()

    async def _claim_jobs(self, *, business_id: UUID, now: datetime) -> list[UUID]:
        stale_before = now - timedelta(minutes=5)
        async with async_session_factory() as session:
            stale_jobs = list(
                (
                    await session.scalars(
                        select(NotificationJob)
                        .where(
                            NotificationJob.business_id == business_id,
                            NotificationJob.state == NotificationJobState.PROCESSING.value,
                            NotificationJob.updated_at < stale_before,
                        )
                        .with_for_update(skip_locked=True)
                        .limit(self._settings.notification_batch_size)
                    )
                ).all()
            )
            for job in stale_jobs:
                job.state = (
                    NotificationJobState.FAILED.value
                    if job.attempt_count >= self._settings.notification_max_attempts
                    else NotificationJobState.PENDING.value
                )
                job.claim_token = None
                job.scheduled_for = now
                job.last_error = "Recovered stale processing job; delivery may have occurred"
                job.updated_at = now
                logger.info("Stale job recovered job_id=%s state=%s", job.id, job.state)
            await session.flush()
            jobs = list(
                (
                    await session.scalars(
                        select(NotificationJob)
                        .where(
                            NotificationJob.business_id == business_id,
                            NotificationJob.state == NotificationJobState.PENDING.value,
                            NotificationJob.scheduled_for <= now,
                        )
                        .order_by(NotificationJob.scheduled_for)
                        .with_for_update(skip_locked=True)
                        .limit(self._settings.notification_batch_size)
                    )
                ).all()
            )
            claimed = []
            for job in jobs:
                if job.attempt_count >= self._settings.notification_max_attempts:
                    job.state = NotificationJobState.FAILED.value
                    job.last_error = "Maximum delivery attempts reached"
                    job.claim_token = None
                    logger.warning("Job permanently failed job_id=%s", job.id)
                    continue
                job.state = NotificationJobState.PROCESSING.value
                job.claim_token = self._claim_token
                job.attempt_count += 1
                job.updated_at = now
                claimed.append(job.id)
                logger.info("Job claimed job_id=%s attempt=%s", job.id, job.attempt_count)
            await session.commit()
            return claimed

    async def _deliver_job(self, bot: Bot, *, job_id: UUID, now: datetime) -> None:
        async with async_session_factory() as session:
            # Hold the row lock across the bounded send and commit. Stale recovery skips
            # this row, and a delayed former owner cannot send a reclaimed job.
            job = await session.scalar(
                select(NotificationJob)
                .where(
                    NotificationJob.id == job_id,
                    NotificationJob.claim_token == self._claim_token,
                )
                .with_for_update()
            )
            if job is None or job.state != NotificationJobState.PROCESSING.value:
                return
            try:
                if not await self._is_enabled(session, job):
                    job.state = NotificationJobState.CANCELLED.value
                    job.last_error = "Disabled or stale notification"
                    job.claim_token = None
                    await session.commit()
                    return
                payload = await self._build_payload(session, job)
                kwargs = {"chat_id": payload.chat_id, "text": payload.text}
                if payload.reply_markup is not None:
                    kwargs["reply_markup"] = payload.reply_markup
                await bot.send_message(**kwargs)
            except (
                TelegramForbiddenError,
                TelegramBadRequest,
                TelegramNotFound,
                TelegramUnauthorizedError,
                UnsupportedNotificationError,
            ) as exc:
                job.state = NotificationJobState.FAILED.value
                job.last_error = type(exc).__name__
            except Exception as exc:
                job.last_error = type(exc).__name__
                if job.attempt_count >= self._settings.notification_max_attempts:
                    job.state = NotificationJobState.FAILED.value
                else:
                    delays = (15, 60, 300, 900, 3600)
                    delay = delays[min(job.attempt_count - 1, len(delays) - 1)]
                    if isinstance(exc, TelegramRetryAfter):
                        delay = max(delay, exc.retry_after)
                    job.state = NotificationJobState.PENDING.value
                    job.scheduled_for = datetime.now(UTC) + timedelta(seconds=delay)
            else:
                job.state = NotificationJobState.SENT.value
                job.sent_at = datetime.now(UTC)
                job.last_error = None
            job.updated_at = datetime.now(UTC)
            job.claim_token = None
            await session.commit()
            if job.state == NotificationJobState.SENT.value:
                logger.info("Job sent job_id=%s", job.id)
            elif job.state == NotificationJobState.PENDING.value:
                logger.info("Job retry scheduled job_id=%s at=%s", job.id, job.scheduled_for)
            else:
                logger.warning("Job permanently failed job_id=%s reason=%s", job.id, job.last_error)

    async def _is_enabled(self, session: AsyncSession, job: NotificationJob) -> bool:
        if job.kind == "client_price_proposal":
            from booking_bot.services.conversation_context import latest_proposal

            proposal = await latest_proposal(session, job.booking_request_id)
            if (
                proposal is None
                or proposal.status != "pending"
                or str(proposal.id) != (job.payload or {}).get("event_id")
            ):
                return False
        if job.appointment_id is not None and (
            job.kind.startswith("client_reminder_")
            or job.kind in {"master_new_appointment", "client_schedule_changed"}
        ):
            appointment = await session.get(Appointment, job.appointment_id)
            if appointment is None or appointment.status in {
                "cancelled_by_client",
                "cancelled_by_master",
            }:
                return False
            if job.kind.startswith("client_reminder_"):
                entry = await session.get(CalendarEntry, appointment.calendar_entry_id)
                master = await session.get(Master, entry.master_id) if entry is not None else None
                settings = await get_client_reminder_settings(
                    session,
                    business_id=job.business_id,
                    master_user_id=master.user_id if master is not None else None,
                )
                return {
                    "client_reminder_7d": settings.seven_days,
                    "client_reminder_3d": settings.three_days,
                    "client_reminder_day_of": settings.day_of,
                }.get(job.kind, True)
        if job.kind not in MASTER_NOTIFICATION_KINDS:
            return True
        return await master_notifications_enabled(
            session,
            business_id=job.business_id,
            user_id=job.recipient_user_id,
        )

    async def _build_payload(
        self,
        session: AsyncSession,
        job: NotificationJob,
    ) -> DeliveryPayload:
        recipient = await session.get(TelegramUser, job.recipient_user_id)
        business = await session.get(Business, job.business_id)
        if job.kind in REQUEST_NOTIFICATION_TITLES:
            from booking_bot.bot.conversation_ui import cb, keyboard, request_link

            if recipient is None or recipient.telegram_user_id is None or business is None:
                raise UnsupportedNotificationError("Notification context is incomplete")
            if job.booking_request_id is None:
                raise UnsupportedNotificationError("Notification request is missing")
            try:
                request = await get_request(
                    session,
                    business_id=job.business_id,
                    request_id=job.booking_request_id,
                    actor_user_id=recipient.id,
                )
            except ConversationAccessError as exc:
                raise UnsupportedNotificationError("Notification recipient lost access") from exc
            text = (
                f"<b>{REQUEST_NOTIFICATION_TITLES[job.kind]}</b>\n\n"
                f"Услуга: <b>{escape(request.service_name_snapshot)}</b>"
            )
            markup = request_link(request.id, dialog="conversation_message" in job.kind)
            if job.kind == "client_price_proposal":
                proposals = await PriceProposalService().list_proposals(
                    session,
                    business_id=job.business_id,
                    request_id=request.id,
                    actor_user_id=recipient.id,
                    limit=1,
                )
                if proposals and proposals[0].status == "pending":
                    proposal = proposals[0]
                    text += f"\n\n{format_money(proposal.amount_minor, proposal.currency)}"
                    if proposal.comment:
                        text += f"\n{escape(proposal.comment)}"
                    markup = keyboard(
                        ("Принять", cb("accept", proposal.id)),
                        ("Обсудить", cb("chat", request.id)),
                        ("Открыть заявку", cb("view", request.id)),
                    )
            return DeliveryPayload(
                chat_id=recipient.telegram_user_id, text=text, reply_markup=markup
            )
        appointment = (
            await session.get(Appointment, job.appointment_id)
            if job.appointment_id is not None
            else None
        )
        if (
            recipient is None
            or recipient.telegram_user_id is None
            or business is None
            or appointment is None
        ):
            raise UnsupportedNotificationError("Notification context is incomplete")

        entry = await session.get(CalendarEntry, appointment.calendar_entry_id)
        master = await session.get(Master, entry.master_id) if entry is not None else None
        location = (
            await session.get(Location, entry.location_id)
            if entry is not None and entry.location_id is not None
            else None
        )
        if master is None:
            raise UnsupportedNotificationError("Appointment master is missing")
        timezone = ZoneInfo(master.timezone or business.timezone)
        local_start = appointment.service_starts_at.astimezone(timezone)
        location_text = f"\nАдрес: <b>{escape(location.name)}</b>" if location else ""

        if job.kind == "master_new_appointment":
            client_name = escape(appointment.client_name_snapshot or "Клиент")
            phone = escape(appointment.client_phone_snapshot or "не указан")
            text = (
                "🔔 <b>Новая запись</b>\n\n"
                f"Услуга: <b>{escape(appointment.service_name_snapshot)}</b>\n"
                f"Дата и время: <b>{local_start:%d.%m.%Y %H:%M}</b>\n"
                f"Клиент: <b>{client_name}</b>\n"
                f"Телефон: <code>{phone}</code>"
                f"{location_text}"
            )
        elif job.kind == "master_appointment_cancelled_by_client":
            client_name = escape(appointment.client_name_snapshot or "Клиент")
            phone = escape(appointment.client_phone_snapshot or "не указан")
            text = (
                "❌ <b>Клиент отменил запись</b>\n\n"
                f"Услуга: <b>{escape(appointment.service_name_snapshot)}</b>\n"
                f"Дата и время: <b>{local_start:%d.%m.%Y %H:%M}</b>\n"
                f"Клиент: <b>{client_name}</b>\n"
                f"Телефон: <code>{phone}</code>"
                f"{location_text}"
            )
        elif job.kind == "master_appointment_rescheduled_by_client":
            client_name = escape(appointment.client_name_snapshot or "Клиент")
            phone = escape(appointment.client_phone_snapshot or "не указан")
            text = (
                "🔄 <b>Клиент перенёс запись</b>\n\n"
                f"Услуга: <b>{escape(appointment.service_name_snapshot)}</b>\n"
                f"Новое время: <b>{local_start:%d.%m.%Y %H:%M}</b>\n"
                f"Клиент: <b>{client_name}</b>\n"
                f"Телефон: <code>{phone}</code>"
                f"{location_text}"
            )
        elif job.kind in CLIENT_REMINDER_KINDS:
            text = (
                get_specialist_template().text(
                    "reminder_title",
                    "⏰ <b>Напоминание о записи</b>",
                    specialist_name=escape(master.display_name),
                )
                + "\n\n"
                f"Услуга: <b>{escape(appointment.service_name_snapshot)}</b>\n"
                f"Дата и время: <b>{local_start:%d.%m.%Y %H:%M}</b>"
                f"{location_text}"
            )
        elif job.kind == "client_appointment_cancelled":
            text = (
                get_specialist_template().text(
                    "appointment_cancelled",
                    "Запись отменена специалистом.",
                    specialist_name=escape(master.display_name),
                )
                + "\n\n"
                f"Услуга: <b>{escape(appointment.service_name_snapshot)}</b>\n"
                f"Дата и время: <b>{local_start:%d.%m.%Y %H:%M}</b>\n\n"
                "Для выбора нового времени откройте /start."
            )
        elif job.kind == "client_appointment_confirmed":
            text = (
                get_specialist_template().text(
                    "appointment_confirmed",
                    "✅ <b>Запись подтверждена</b>",
                    specialist_name=escape(master.display_name),
                )
                + "\n\n"
                f"Услуга: <b>{escape(appointment.service_name_snapshot)}</b>\n"
                f"Дата и время: <b>{local_start:%d.%m.%Y %H:%M}</b>"
                f"{location_text}"
            )
        elif job.kind == "client_appointment_rescheduled":
            text = (
                get_specialist_template().text(
                    "appointment_rescheduled",
                    "🔄 <b>Специалист перенёс запись</b>",
                    specialist_name=escape(master.display_name),
                )
                + "\n\n"
                f"Услуга: <b>{escape(appointment.service_name_snapshot)}</b>\n"
                f"Новое время: <b>{local_start:%d.%m.%Y %H:%M}</b>"
                f"{location_text}\n\n"
                "Если новое время не подходит, свяжитесь со специалистом."
            )
        elif job.kind == "client_schedule_changed":
            text = (
                "🗓 <b>Изменилось расписание специалиста</b>\n\n"
                f"У специалиста <b>{escape(master.display_name)}</b> изменился рабочий график.\n"
                "Ваша запись остаётся в силе:\n\n"
                f"Услуга: <b>{escape(appointment.service_name_snapshot)}</b>\n"
                f"Дата и время: <b>{local_start:%d.%m.%Y %H:%M}</b>"
                f"{location_text}\n\n"
                "Если время записи изменится или запись будет отменена, "
                "вы получите отдельное уведомление."
            )
        else:
            raise UnsupportedNotificationError(f"Unsupported notification kind: {job.kind}")
        return DeliveryPayload(chat_id=recipient.telegram_user_id, text=text)

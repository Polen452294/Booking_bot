"""Bounded operator reads and explicit retries; no Telegram calls or payload output."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from booking_bot.db.models.notifications import AuditLog, NotificationJob

# Unknown errors and ambiguous delivery outcomes are deliberately not replayed.
RETRYABLE_ERRORS = frozenset(
    {
        "TelegramNetworkError",
        "TelegramRetryAfter",
        "TelegramServerError",
        "TimeoutError",
        "ConnectionError",
        "ConnectionResetError",
    }
)
PERMANENT_ERRORS = frozenset(
    {
        "TelegramForbiddenError",
        "TelegramBadRequest",
        "TelegramNotFound",
        "TelegramUnauthorizedError",
        "UnsupportedNotificationError",
    }
)


async def queue_counts(session: AsyncSession, business_id: UUID) -> dict[str, int]:
    states = ("pending", "processing", "failed")
    rows = await session.execute(
        select(NotificationJob.state, func.count())
        .where(NotificationJob.business_id == business_id, NotificationJob.state.in_(states))
        .group_by(NotificationJob.state)
    )
    counts = dict(rows.all())
    return {state: counts.get(state, 0) for state in states}


async def failed_jobs(session: AsyncSession, business_id: UUID) -> list[dict]:
    jobs = await session.scalars(
        select(NotificationJob)
        .where(NotificationJob.business_id == business_id, NotificationJob.state == "failed")
        .order_by(NotificationJob.created_at.desc(), NotificationJob.id)
        .limit(100)
    )
    return [
        {
            "job_id": str(job.id),
            "kind": job.kind,
            "attempts": job.attempt_count,
            "last_error": (
                job.last_error
                if job.last_error in RETRYABLE_ERRORS | PERMANENT_ERRORS
                else "Unclassified error (private details omitted)"
            ),
            "created_at": job.created_at.isoformat(),
            "retryable": job.last_error in RETRYABLE_ERRORS,
        }
        for job in jobs
    ]


async def retry_jobs(session: AsyncSession, business_id: UUID, job_id: UUID | None = None) -> dict:
    from booking_bot.config import get_settings
    from booking_bot.services.notification_delivery import (
        NotificationDeliveryService,
        UnsupportedNotificationError,
    )

    query = select(NotificationJob).where(
        NotificationJob.business_id == business_id,
        NotificationJob.state == "failed",
        NotificationJob.last_error.in_(RETRYABLE_ERRORS),
    )
    if job_id is not None:
        query = query.where(NotificationJob.id == job_id)
    jobs = await session.scalars(
        query.order_by(NotificationJob.created_at, NotificationJob.id)
        .limit(100)
        .with_for_update(skip_locked=True)
    )
    delivery = NotificationDeliveryService(get_settings())
    now = datetime.now(UTC)
    retried, refused = [], []
    for job in jobs:
        # A delayed reminder or cancelled booking must not be replayed.
        allowed = await delivery._is_enabled(session, job)
        if job.kind.startswith("client_reminder_"):
            from booking_bot.db.models import Appointment

            appointment = await session.get(Appointment, job.appointment_id)
            allowed = allowed and appointment is not None and appointment.service_starts_at > now
        if not allowed:
            refused.append(str(job.id))
            continue
        try:
            await delivery._build_payload(session, job)
        except UnsupportedNotificationError:
            refused.append(str(job.id))
            continue
        session.add(
            AuditLog(
                business_id=business_id,
                action="notification_retry",
                entity_type="notification_job",
                entity_id=job.id,
                details={"attempts_before": job.attempt_count, "error_type": job.last_error},
            )
        )
        job.state = "pending"
        job.attempt_count = 0
        job.claim_token = None
        job.scheduled_for = now
        job.updated_at = now
        # Preserve the classified reason until the next delivery result.
        retried.append(str(job.id))
    if job_id is not None and not retried:
        raise ValueError("Job is not an eligible failed job in this deployment")
    return {"retried": retried, "refused": refused, "batch_limit": 100}

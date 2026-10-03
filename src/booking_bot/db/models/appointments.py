from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    column,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from booking_bot.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from booking_bot.domain.enums import (
    AppointmentStatus,
    CalendarEntryKind,
    CalendarEntryState,
    HoldStatus,
)


class CalendarEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "calendar_entries"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="calendar_interval_positive"),
        ExcludeConstraint(
            (column("master_id"), "="),
            (
                func.tstzrange(column("starts_at"), column("ends_at"), "[)"),
                "&&",
            ),
            where=text("state = 'active'"),
            using="gist",
            name="ex_calendar_entries_no_active_overlap",
        ),
        Index("ix_calendar_entries_master_starts_at", "master_id", "starts_at"),
    )

    business_id: Mapped[UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    master_id: Mapped[UUID] = mapped_column(
        ForeignKey("masters.id", ondelete="CASCADE"), nullable=False
    )
    location_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("locations.id", ondelete="SET NULL"), index=True
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(24), nullable=False, default=CalendarEntryKind.HOLD.value
    )
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, default=CalendarEntryState.ACTIVE.value
    )


class SlotHold(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "slot_holds"

    business_id: Mapped[UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    calendar_entry_id: Mapped[UUID] = mapped_column(
        ForeignKey("calendar_entries.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    service_starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    service_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=HoldStatus.ACTIVE.value)


class TimeBlock(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "time_blocks"

    business_id: Mapped[UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    calendar_entry_id: Mapped[UUID] = mapped_column(
        ForeignKey("calendar_entries.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    reason: Mapped[str | None] = mapped_column(String(255))


class Appointment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint("duration_minutes > 0", name="appointment_duration_positive"),
        CheckConstraint(
            "price_minor IS NULL OR price_minor >= 0",
            name="appointment_price_nonnegative",
        ),
    )

    business_id: Mapped[UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    calendar_entry_id: Mapped[UUID] = mapped_column(
        ForeignKey("calendar_entries.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    service_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("services.id", ondelete="SET NULL"), index=True
    )
    booking_request_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("booking_requests.id", ondelete="RESTRICT"), unique=True
    )
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AppointmentStatus.CONFIRMED.value, index=True
    )
    service_name_snapshot: Mapped[str] = mapped_column(String(160), nullable=False)
    service_starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    service_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    price_minor: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB")
    client_name_snapshot: Mapped[str | None] = mapped_column(String(160))
    client_phone_snapshot: Mapped[str | None] = mapped_column(String(32))
    client_comment: Mapped[str | None] = mapped_column(Text)
    internal_note: Mapped[str | None] = mapped_column(Text)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class AppointmentHistory(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "appointment_history"

    business_id: Mapped[UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    appointment_id: Mapped[UUID] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="SET NULL"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    event_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

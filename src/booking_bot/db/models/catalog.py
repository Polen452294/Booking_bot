from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from booking_bot.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from booking_bot.domain.enums import PricingMode


class Location(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "locations"

    business_id: Mapped[UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Service(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "services"
    __table_args__ = (
        UniqueConstraint("business_id", "config_key"),
        CheckConstraint("duration_minutes > 0", name="duration_positive"),
        CheckConstraint("buffer_before_minutes >= 0", name="buffer_before_nonnegative"),
        CheckConstraint("buffer_after_minutes >= 0", name="buffer_after_nonnegative"),
        CheckConstraint("price_minor IS NULL OR price_minor >= 0", name="price_nonnegative"),
        CheckConstraint("pricing_mode IN ('fixed', 'from', 'negotiable')", name="pricing_mode"),
    )

    business_id: Mapped[UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    config_key: Mapped[str | None] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    buffer_before_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    buffer_after_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    price_minor: Mapped[int | None] = mapped_column(Integer)
    pricing_mode: Mapped[str] = mapped_column(
        String(24), nullable=False, default=PricingMode.FIXED.value, server_default="fixed"
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB")
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    requires_deposit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deposit_minor: Mapped[int | None] = mapped_column(Integer)
    is_owner_managed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class MasterService(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "master_services"
    __table_args__ = (
        UniqueConstraint("master_id", "service_id"),
        CheckConstraint(
            "duration_override_minutes IS NULL OR duration_override_minutes > 0",
            name="duration_override_positive",
        ),
        CheckConstraint(
            "price_override_minor IS NULL OR price_override_minor >= 0",
            name="price_override_nonnegative",
        ),
    )

    business_id: Mapped[UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    master_id: Mapped[UUID] = mapped_column(
        ForeignKey("masters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True
    )
    duration_override_minutes: Mapped[int | None] = mapped_column(Integer)
    price_override_minor: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

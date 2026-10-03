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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from booking_bot.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from booking_bot.domain.enums import (
    BookingRequestStatus,
    ConversationStatus,
    PriceProposalStatus,
)


class BookingRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "booking_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'waiting_master', 'waiting_client', 'terms_proposed', "
            "'terms_accepted', 'booked', 'cancelled', 'closed')",
            name="status",
        ),
        CheckConstraint("length(description) BETWEEN 1 AND 4000", name="description_length"),
        Index("ix_booking_requests_business_status_created", "business_id", "status", "created_at"),
        Index("ix_booking_requests_client_created", "client_user_id", "created_at"),
        Index("ix_booking_requests_master_created", "master_id", "created_at"),
    )

    business_id: Mapped[UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    master_id: Mapped[UUID] = mapped_column(ForeignKey("masters.id", ondelete="RESTRICT"))
    client_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="RESTRICT")
    )
    service_id: Mapped[UUID] = mapped_column(ForeignKey("services.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=BookingRequestStatus.DRAFT.value
    )
    service_name_snapshot: Mapped[str] = mapped_column(String(160))
    client_name_snapshot: Mapped[str] = mapped_column(String(160))
    client_phone_snapshot: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'closed')", name="status"),
        CheckConstraint("last_message_sequence >= 0", name="sequence_nonnegative"),
    )

    booking_request_id: Mapped[UUID] = mapped_column(
        ForeignKey("booking_requests.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=ConversationStatus.OPEN.value
    )
    last_message_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationMessage(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence"),
        Index("ix_conversation_messages_conversation_created", "conversation_id", "created_at"),
        CheckConstraint("sequence > 0", name="sequence_positive"),
        CheckConstraint("sender_role IN ('client', 'master', 'system')", name="sender_role"),
        CheckConstraint("message_type IN ('text', 'photo', 'document', 'system')", name="type"),
        CheckConstraint("text IS NULL OR length(text) <= 4000", name="text_length"),
        CheckConstraint(
            "(message_type = 'system' AND sender_role = 'system' AND event_type IS NOT NULL "
            "AND event_payload IS NOT NULL AND telegram_file_id IS NULL) OR "
            "(message_type != 'system' AND sender_role != 'system' AND sender_user_id IS NOT NULL "
            "AND event_type IS NULL AND event_payload IS NULL AND "
            "((message_type = 'text' AND text IS NOT NULL AND length(text) > 0 "
            "AND telegram_file_id IS NULL AND telegram_file_unique_id IS NULL) OR "
            "(message_type IN ('photo', 'document') AND telegram_file_id IS NOT NULL "
            "AND telegram_file_unique_id IS NOT NULL)))",
            name="content",
        ),
    )

    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    sender_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="RESTRICT")
    )
    sender_role: Mapped[str] = mapped_column(String(24))
    message_type: Mapped[str] = mapped_column(String(24))
    text: Mapped[str | None] = mapped_column(Text)
    telegram_file_id: Mapped[str | None] = mapped_column(String(512))
    telegram_file_unique_id: Mapped[str | None] = mapped_column(String(256))
    event_type: Mapped[str | None] = mapped_column(String(100))
    event_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationReadState(Base):
    __tablename__ = "conversation_read_states"
    __table_args__ = (CheckConstraint("last_read_sequence >= 0", name="sequence_nonnegative"),)

    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="CASCADE"), primary_key=True
    )
    last_read_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PriceProposal(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "price_proposals"
    __table_args__ = (
        UniqueConstraint("booking_request_id", "revision"),
        Index("ix_price_proposals_request_status", "booking_request_id", "status"),
        Index(
            "uq_price_proposals_pending",
            "booking_request_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        CheckConstraint("amount_minor BETWEEN 0 AND 2000000000", name="amount"),
        CheckConstraint("revision > 0", name="revision"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        CheckConstraint("comment IS NULL OR length(comment) <= 2000", name="comment_length"),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected', 'superseded')", name="status"
        ),
    )

    booking_request_id: Mapped[UUID] = mapped_column(
        ForeignKey("booking_requests.id", ondelete="CASCADE")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="RESTRICT")
    )
    revision: Mapped[int] = mapped_column(Integer)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    comment: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=PriceProposalStatus.PENDING.value
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

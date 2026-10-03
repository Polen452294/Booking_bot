from enum import StrEnum


class MemberRole(StrEnum):
    CLIENT = "client"
    MASTER = "master"
    MANAGER = "manager"
    OWNER = "owner"


class AppointmentStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    PENDING_PAYMENT = "pending_payment"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED_BY_CLIENT = "cancelled_by_client"
    CANCELLED_BY_MASTER = "cancelled_by_master"
    NO_SHOW = "no_show"


class CalendarEntryKind(StrEnum):
    HOLD = "hold"
    APPOINTMENT = "appointment"
    BLOCK = "block"


class CalendarEntryState(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"


class HoldStatus(StrEnum):
    ACTIVE = "active"
    CONVERTED = "converted"
    EXPIRED = "expired"
    RELEASED = "released"


class ScheduleExceptionKind(StrEnum):
    DAY_OFF = "day_off"
    CUSTOM_HOURS = "custom_hours"
    EXTRA_DAY = "extra_day"


class NotificationJobState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PricingMode(StrEnum):
    FIXED = "fixed"
    FROM = "from"
    NEGOTIABLE = "negotiable"


class BookingRequestStatus(StrEnum):
    DRAFT = "draft"
    WAITING_MASTER = "waiting_master"
    WAITING_CLIENT = "waiting_client"
    TERMS_PROPOSED = "terms_proposed"
    TERMS_ACCEPTED = "terms_accepted"
    BOOKED = "booked"
    CANCELLED = "cancelled"
    CLOSED = "closed"


class ConversationStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class ConversationSenderRole(StrEnum):
    CLIENT = "client"
    MASTER = "master"
    SYSTEM = "system"


class ConversationMessageType(StrEnum):
    TEXT = "text"
    PHOTO = "photo"
    DOCUMENT = "document"
    SYSTEM = "system"


class PriceProposalStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"

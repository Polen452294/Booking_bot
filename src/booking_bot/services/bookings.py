from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from booking_bot.config import Settings
from booking_bot.db.models import (
    Appointment,
    AppointmentHistory,
    BookingRequest,
    Business,
    CalendarEntry,
    Location,
    Master,
    MasterService,
    NotificationJob,
    PriceProposal,
    Service,
    SlotHold,
    TelegramUser,
)
from booking_bot.domain.enums import (
    AppointmentStatus,
    CalendarEntryKind,
    CalendarEntryState,
    HoldStatus,
    NotificationJobState,
    PricingMode,
)
from booking_bot.services.availability import AvailabilityService, BookableSlot
from booking_bot.services.reminder_settings import get_client_reminder_settings
from booking_bot.services.users import normalize_phone


class SlotUnavailableError(RuntimeError):
    pass


class HoldNotFoundError(LookupError):
    pass


class HoldExpiredError(RuntimeError):
    pass


class ClientPhoneRequiredError(RuntimeError):
    pass


class AppointmentNotFoundError(LookupError):
    pass


class AppointmentChangeNotAllowedError(RuntimeError):
    pass


class ManualClientValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HoldSummary:
    hold_id: UUID
    business_name: str
    service_name: str
    master_name: str
    location_name: str | None
    local_start: datetime
    local_end: datetime
    expires_at: datetime
    client_phone: str | None


@dataclass(frozen=True, slots=True)
class AppointmentSummary:
    appointment_id: UUID
    service_id: UUID | None
    service_name: str
    master_name: str
    location_name: str | None
    location_address: str | None
    local_start: datetime
    local_end: datetime
    status: str
    can_change: bool
    change_deadline: datetime
    booking_request_id: UUID | None = None


class BookingService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._availability = AvailabilityService(settings)

    async def create_hold(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        master_id: UUID,
        service_id: UUID,
        client_id: UUID,
        service_start: datetime,
        local_date: date,
        now: datetime | None = None,
        respect_min_lead_time: bool = True,
        duration_minutes_override: int | None = None,
    ) -> SlotHold:
        now = now or datetime.now(UTC)
        if service_start.tzinfo is None:
            raise ValueError("service_start must be timezone-aware")

        slots = await self._availability.list_slots(
            session,
            business_id=business_id,
            master_id=master_id,
            service_id=service_id,
            local_date=local_date,
            now=now,
            respect_min_lead_time=respect_min_lead_time,
            duration_minutes_override=duration_minutes_override,
        )
        selected = self._find_slot(slots, service_start.astimezone(UTC))
        if selected is None:
            raise SlotUnavailableError("The selected slot is no longer available")

        calendar_entry = CalendarEntry(
            business_id=business_id,
            master_id=master_id,
            location_id=selected.location_id,
            starts_at=selected.occupied_start,
            ends_at=selected.occupied_end,
            kind=CalendarEntryKind.HOLD.value,
            state=CalendarEntryState.ACTIVE.value,
        )
        hold = SlotHold(
            business_id=business_id,
            calendar_entry_id=calendar_entry.id,
            service_id=service_id,
            client_id=client_id,
            expires_at=now + timedelta(minutes=self._settings.slot_hold_minutes),
            service_starts_at=selected.service_start,
            service_ends_at=selected.service_end,
            status=HoldStatus.ACTIVE.value,
        )
        try:
            async with session.begin_nested():
                session.add(calendar_entry)
                await session.flush()
                hold.calendar_entry_id = calendar_entry.id
                session.add(hold)
                await session.flush()
        except IntegrityError as exc:
            raise SlotUnavailableError("The selected slot was just booked") from exc
        except DBAPIError as exc:
            # Concurrent GiST exclusion inserts can deadlock rather than return
            # an exclusion violation. The savepoint has rolled back this hold;
            # reuse the existing slot-conflict outcome without hiding DB outages.
            if getattr(exc.orig, "sqlstate", None) != "40P01":
                raise
            raise SlotUnavailableError("The selected slot was just booked") from exc
        return hold

    async def get_hold_summary(
        self,
        session: AsyncSession,
        *,
        hold_id: UUID,
        client_id: UUID,
    ) -> HoldSummary:
        row = (
            await session.execute(
                select(SlotHold, CalendarEntry, Service, Master, Business, Location)
                .join(CalendarEntry, CalendarEntry.id == SlotHold.calendar_entry_id)
                .join(Service, Service.id == SlotHold.service_id)
                .join(Master, Master.id == CalendarEntry.master_id)
                .join(Business, Business.id == SlotHold.business_id)
                .outerjoin(Location, Location.id == CalendarEntry.location_id)
                .where(SlotHold.id == hold_id, SlotHold.client_id == client_id)
            )
        ).one_or_none()
        if row is None:
            raise HoldNotFoundError

        hold, _entry, service, master, business, location = row
        client = await session.get(TelegramUser, client_id)
        timezone = ZoneInfo(master.timezone or business.timezone)
        return HoldSummary(
            hold_id=hold.id,
            business_name=business.name,
            service_name=service.name,
            master_name=master.display_name,
            location_name=location.name if location else None,
            local_start=hold.service_starts_at.astimezone(timezone),
            local_end=hold.service_ends_at.astimezone(timezone),
            expires_at=hold.expires_at,
            client_phone=client.phone if client else None,
        )

    async def confirm_hold(
        self,
        session: AsyncSession,
        *,
        hold_id: UUID,
        client_id: UUID,
        now: datetime | None = None,
        created_by_user_id: UUID | None = None,
        force_confirmed: bool = False,
        notify_master: bool = True,
        client_comment: str | None = None,
        booking_request_id: UUID | None = None,
    ) -> AppointmentSummary:
        arguments = dict(
            hold_id=hold_id,
            client_id=client_id,
            now=now,
            created_by_user_id=created_by_user_id,
            force_confirmed=force_confirmed,
            notify_master=notify_master,
            client_comment=client_comment,
        )
        if booking_request_id is None:
            return await self._confirm_hold(session, **arguments)

        from booking_bot.services.booking_requests import accepted_booking_context

        # Lock the request before the hold, matching every request mutation.
        async with session.begin_nested():
            business_id = await session.scalar(
                select(SlotHold.business_id).where(
                    SlotHold.id == hold_id,
                    SlotHold.client_id == client_id,
                )
            )
            if business_id is None:
                raise HoldNotFoundError
            request, proposal = await accepted_booking_context(
                session,
                business_id=business_id,
                request_id=booking_request_id,
                client_id=client_id,
            )
            if created_by_user_id is not None and created_by_user_id != client_id:
                raise AppointmentChangeNotAllowedError(
                    "Request booking must be confirmed by client"
                )
            return await self._confirm_hold(
                session,
                **arguments,
                request=request,
                proposal=proposal,
            )

    async def _confirm_hold(
        self,
        session: AsyncSession,
        *,
        hold_id: UUID,
        client_id: UUID,
        now: datetime | None = None,
        created_by_user_id: UUID | None = None,
        force_confirmed: bool = False,
        notify_master: bool = True,
        client_comment: str | None = None,
        request: BookingRequest | None = None,
        proposal: PriceProposal | None = None,
    ) -> AppointmentSummary:
        now = now or datetime.now(UTC)
        hold = await session.scalar(
            select(SlotHold)
            .where(SlotHold.id == hold_id, SlotHold.client_id == client_id)
            .with_for_update()
        )
        if hold is None:
            raise HoldNotFoundError

        if hold.status != HoldStatus.ACTIVE.value:
            # Replayed callbacks must never expire an already converted appointment.
            raise HoldExpiredError("The slot hold is no longer active")
        entry = await session.get(CalendarEntry, hold.calendar_entry_id)
        if (
            hold.expires_at <= now
            or entry is None
            or entry.state != CalendarEntryState.ACTIVE.value
        ):
            if entry is not None and entry.state == CalendarEntryState.ACTIVE.value:
                entry.state = CalendarEntryState.EXPIRED.value
            hold.status = HoldStatus.EXPIRED.value
            await session.flush()
            raise HoldExpiredError("The slot hold has expired")

        service = await session.get(Service, hold.service_id)
        master = await session.get(Master, entry.master_id)
        business = await session.get(Business, hold.business_id)
        client = await session.get(TelegramUser, client_id)
        location = (
            await session.get(Location, entry.location_id)
            if entry.location_id is not None
            else None
        )
        if service is None or master is None or business is None or client is None:
            raise HoldNotFoundError
        if request is not None and (
            request.business_id != business.id
            or request.master_id != master.id
            or request.service_id != service.id
            or request.client_user_id != client.id
        ):
            raise AppointmentChangeNotAllowedError("The hold does not match the request")
        if request is None and service.pricing_mode == PricingMode.NEGOTIABLE.value:
            raise AppointmentChangeNotAllowedError("Negotiable services require accepted terms")
        if not client.phone and request is None:
            raise ClientPhoneRequiredError("Client phone is required")

        master_service = await session.scalar(
            select(MasterService).where(
                MasterService.master_id == master.id,
                MasterService.service_id == service.id,
            )
        )
        price_minor = (
            master_service.price_override_minor
            if master_service and master_service.price_override_minor is not None
            else service.price_minor
        )
        appointment_status = AppointmentStatus.CONFIRMED.value
        if service.requires_approval and not force_confirmed:
            appointment_status = AppointmentStatus.PENDING_APPROVAL.value
        actor_user_id = created_by_user_id or client.id

        entry.kind = CalendarEntryKind.APPOINTMENT.value
        hold.status = HoldStatus.CONVERTED.value
        client_name = (
            " ".join(part for part in (client.first_name, client.last_name) if part) or None
        )
        appointment = Appointment(
            business_id=business.id,
            calendar_entry_id=entry.id,
            service_id=service.id,
            client_id=client.id,
            created_by_user_id=actor_user_id,
            status=appointment_status,
            service_name_snapshot=service.name,
            service_starts_at=hold.service_starts_at,
            service_ends_at=hold.service_ends_at,
            duration_minutes=int(
                (hold.service_ends_at - hold.service_starts_at).total_seconds() // 60
            ),
            price_minor=proposal.amount_minor if proposal else price_minor,
            currency=proposal.currency if proposal else service.currency,
            booking_request_id=request.id if request else None,
            client_name_snapshot=request.client_name_snapshot if request else client_name,
            client_phone_snapshot=request.client_phone_snapshot if request else client.phone,
            client_comment=client_comment,
        )
        session.add(appointment)
        await session.flush()
        if request is not None:
            from booking_bot.domain.enums import BookingRequestStatus
            from booking_bot.services.conversation_context import record_event, transition

            transition(request, BookingRequestStatus.BOOKED)
            await record_event(
                session,
                request=request,
                actor_user_id=client.id,
                action="appointment_created",
                details={
                    "appointment_id": str(appointment.id),
                    "proposal_id": str(proposal.id),
                    "amount_minor": appointment.price_minor,
                    "currency": appointment.currency,
                    "starts_at": appointment.service_starts_at.isoformat(),
                },
            )
        session.add(
            AppointmentHistory(
                business_id=business.id,
                appointment_id=appointment.id,
                actor_user_id=actor_user_id,
                event_type=(
                    "created_by_master" if actor_user_id != client.id else "created_from_hold"
                ),
                to_status=appointment_status,
            )
        )
        await self._schedule_notifications(
            session,
            appointment=appointment,
            client=client,
            master=master,
            business=business,
            now=now,
            notify_master=notify_master,
        )
        await session.flush()

        timezone = ZoneInfo(master.timezone or business.timezone)
        return AppointmentSummary(
            appointment_id=appointment.id,
            booking_request_id=appointment.booking_request_id,
            service_id=appointment.service_id,
            service_name=appointment.service_name_snapshot,
            master_name=master.display_name,
            location_name=location.name if location else None,
            location_address=location.address if location else None,
            local_start=appointment.service_starts_at.astimezone(timezone),
            local_end=appointment.service_ends_at.astimezone(timezone),
            status=appointment.status,
            can_change=self._can_change(appointment, now),
            change_deadline=appointment.service_starts_at.astimezone(timezone)
            - timedelta(hours=self._settings.cancellation_cutoff_hours),
        )

    async def confirm_manual_hold(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        hold_id: UUID,
        holder_user_id: UUID,
        actor_user_id: UUID,
        client_name: str,
        client_phone: str,
        client_comment: str | None = None,
        now: datetime | None = None,
    ) -> AppointmentSummary:
        now = now or datetime.now(UTC)
        normalized_name = " ".join(client_name.split())
        normalized_phone = normalize_phone(client_phone)
        normalized_comment = client_comment.strip() if client_comment else None
        if not 2 <= len(normalized_name) <= 160:
            raise ManualClientValidationError("Client name must contain 2-160 characters")
        if normalized_phone is None:
            raise ManualClientValidationError("Client phone must contain 5-32 characters")
        if normalized_comment and len(normalized_comment) > 2000:
            raise ManualClientValidationError("Client comment is too long")

        hold = await session.scalar(
            select(SlotHold)
            .where(
                SlotHold.id == hold_id,
                SlotHold.business_id == business_id,
                SlotHold.client_id == holder_user_id,
            )
            .with_for_update()
        )
        if hold is None:
            raise HoldNotFoundError
        entry = await session.get(CalendarEntry, hold.calendar_entry_id)
        if hold.status != HoldStatus.ACTIVE.value:
            raise HoldExpiredError("The slot hold is no longer active")
        if (
            hold.expires_at <= now
            or entry is None
            or entry.state != CalendarEntryState.ACTIVE.value
        ):
            if entry is not None and entry.state == CalendarEntryState.ACTIVE.value:
                entry.state = CalendarEntryState.EXPIRED.value
            hold.status = HoldStatus.EXPIRED.value
            await session.flush()
            raise HoldExpiredError("The slot hold has expired")

        manual_client = TelegramUser(
            telegram_user_id=None,
            first_name=normalized_name,
            phone=normalized_phone,
        )
        session.add(manual_client)
        await session.flush()
        hold.client_id = manual_client.id
        await session.flush()
        return await self.confirm_hold(
            session,
            hold_id=hold.id,
            client_id=manual_client.id,
            now=now,
            created_by_user_id=actor_user_id,
            force_confirmed=True,
            notify_master=False,
            client_comment=normalized_comment,
        )

    async def release_hold(
        self,
        session: AsyncSession,
        *,
        hold_id: UUID,
        client_id: UUID,
    ) -> None:
        hold = await session.scalar(
            select(SlotHold).where(
                SlotHold.id == hold_id,
                SlotHold.client_id == client_id,
                SlotHold.status == HoldStatus.ACTIVE.value,
            )
        )
        if hold is None:
            return
        entry = await session.get(CalendarEntry, hold.calendar_entry_id)
        hold.status = HoldStatus.RELEASED.value
        if entry is not None:
            entry.state = CalendarEntryState.RELEASED.value
        await session.flush()

    async def list_upcoming(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        client_id: UUID,
        now: datetime | None = None,
    ) -> list[AppointmentSummary]:
        now = now or datetime.now(UTC)
        rows = (
            await session.execute(
                select(Appointment, CalendarEntry, Master, Business, Location)
                .join(CalendarEntry, CalendarEntry.id == Appointment.calendar_entry_id)
                .join(Master, Master.id == CalendarEntry.master_id)
                .join(Business, Business.id == Appointment.business_id)
                .outerjoin(Location, Location.id == CalendarEntry.location_id)
                .where(
                    Appointment.business_id == business_id,
                    Appointment.client_id == client_id,
                    Appointment.service_starts_at >= now,
                    Appointment.status.in_(
                        [
                            AppointmentStatus.PENDING_APPROVAL.value,
                            AppointmentStatus.PENDING_PAYMENT.value,
                            AppointmentStatus.CONFIRMED.value,
                        ]
                    ),
                )
                .order_by(Appointment.service_starts_at)
            )
        ).all()
        return [
            self._appointment_summary(
                appointment=appointment,
                master=master,
                business=business,
                location=location,
                now=now,
            )
            for appointment, _entry, master, business, location in rows
        ]

    async def list_past(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        client_id: UUID,
        now: datetime | None = None,
        limit: int = 20,
    ) -> list[AppointmentSummary]:
        now = now or datetime.now(UTC)
        return await self._list_client_appointments(
            session,
            business_id=business_id,
            client_id=client_id,
            statuses=[
                AppointmentStatus.PENDING_APPROVAL.value,
                AppointmentStatus.PENDING_PAYMENT.value,
                AppointmentStatus.CONFIRMED.value,
                AppointmentStatus.COMPLETED.value,
                AppointmentStatus.NO_SHOW.value,
            ],
            before=now,
            now=now,
            limit=limit,
        )

    async def list_cancelled(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        client_id: UUID,
        now: datetime | None = None,
        limit: int = 20,
    ) -> list[AppointmentSummary]:
        now = now or datetime.now(UTC)
        return await self._list_client_appointments(
            session,
            business_id=business_id,
            client_id=client_id,
            statuses=[
                AppointmentStatus.CANCELLED_BY_CLIENT.value,
                AppointmentStatus.CANCELLED_BY_MASTER.value,
            ],
            now=now,
            limit=limit,
        )

    async def _list_client_appointments(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        client_id: UUID,
        statuses: list[str],
        now: datetime,
        before: datetime | None = None,
        limit: int,
    ) -> list[AppointmentSummary]:
        query = (
            select(Appointment, CalendarEntry, Master, Business, Location)
            .join(CalendarEntry, CalendarEntry.id == Appointment.calendar_entry_id)
            .join(Master, Master.id == CalendarEntry.master_id)
            .join(Business, Business.id == Appointment.business_id)
            .outerjoin(Location, Location.id == CalendarEntry.location_id)
            .where(
                Appointment.business_id == business_id,
                Appointment.client_id == client_id,
                Appointment.status.in_(statuses),
            )
        )
        if before is not None:
            query = query.where(Appointment.service_starts_at < before)
        rows = (
            await session.execute(query.order_by(Appointment.service_starts_at.desc()).limit(limit))
        ).all()
        return [
            self._appointment_summary(
                appointment=appointment,
                master=master,
                business=business,
                location=location,
                now=now,
            )
            for appointment, _entry, master, business, location in rows
        ]

    async def get_appointment(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        client_id: UUID,
        appointment_id: UUID,
        now: datetime | None = None,
    ) -> AppointmentSummary:
        now = now or datetime.now(UTC)
        row = (
            await session.execute(
                select(Appointment, CalendarEntry, Master, Business, Location)
                .join(CalendarEntry, CalendarEntry.id == Appointment.calendar_entry_id)
                .join(Master, Master.id == CalendarEntry.master_id)
                .join(Business, Business.id == Appointment.business_id)
                .outerjoin(Location, Location.id == CalendarEntry.location_id)
                .where(
                    Appointment.id == appointment_id,
                    Appointment.business_id == business_id,
                    Appointment.client_id == client_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise AppointmentNotFoundError
        appointment, _entry, master, business, location = row
        return self._appointment_summary(
            appointment=appointment,
            master=master,
            business=business,
            location=location,
            now=now,
        )

    async def cancel_appointment(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        client_id: UUID,
        appointment_id: UUID,
        now: datetime | None = None,
    ) -> AppointmentSummary:
        now = now or datetime.now(UTC)
        appointment = await self._get_changeable_appointment(
            session,
            business_id=business_id,
            client_id=client_id,
            appointment_id=appointment_id,
            now=now,
        )
        entry = await session.get(CalendarEntry, appointment.calendar_entry_id)
        if entry is None:
            raise AppointmentNotFoundError
        master = await session.get(Master, entry.master_id)
        business = await session.get(Business, appointment.business_id)
        location = (
            await session.get(Location, entry.location_id)
            if entry.location_id is not None
            else None
        )
        if master is None or business is None:
            raise AppointmentNotFoundError

        previous_status = appointment.status
        appointment.status = AppointmentStatus.CANCELLED_BY_CLIENT.value
        appointment.lock_version += 1
        entry.state = CalendarEntryState.RELEASED.value
        await self._cancel_pending_notifications(
            session,
            appointment_id=appointment.id,
        )
        if master.user_id is not None:
            self._schedule_master_notification(
                session,
                appointment=appointment,
                master=master,
                business=business,
                kind="master_appointment_cancelled_by_client",
                now=now,
            )
        session.add(
            AppointmentHistory(
                business_id=business.id,
                appointment_id=appointment.id,
                actor_user_id=client_id,
                event_type="cancelled_by_client",
                from_status=previous_status,
                to_status=appointment.status,
            )
        )
        await session.flush()
        return self._appointment_summary(
            appointment=appointment,
            master=master,
            business=business,
            location=location,
            now=now,
        )

    async def confirm_reschedule(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        client_id: UUID,
        appointment_id: UUID,
        hold_id: UUID,
        now: datetime | None = None,
    ) -> AppointmentSummary:
        now = now or datetime.now(UTC)
        appointment = await self._get_changeable_appointment(
            session,
            business_id=business_id,
            client_id=client_id,
            appointment_id=appointment_id,
            now=now,
        )
        hold = await session.scalar(
            select(SlotHold)
            .where(
                SlotHold.id == hold_id,
                SlotHold.business_id == business_id,
                SlotHold.client_id == client_id,
            )
            .with_for_update()
        )
        if hold is None:
            raise HoldNotFoundError

        if hold.status != HoldStatus.ACTIVE.value:
            raise HoldExpiredError("The slot hold is no longer active")
        old_entry = await session.get(CalendarEntry, appointment.calendar_entry_id)
        new_entry = await session.get(CalendarEntry, hold.calendar_entry_id)
        if (
            hold.expires_at <= now
            or new_entry is None
            or new_entry.state != CalendarEntryState.ACTIVE.value
        ):
            if new_entry is not None and new_entry.state == CalendarEntryState.ACTIVE.value:
                new_entry.state = CalendarEntryState.EXPIRED.value
            hold.status = HoldStatus.EXPIRED.value
            await session.flush()
            raise HoldExpiredError("The slot hold has expired")
        if (
            old_entry is None
            or hold.service_id != appointment.service_id
            or new_entry.master_id != old_entry.master_id
        ):
            raise AppointmentChangeNotAllowedError("The hold does not match the appointment")

        master = await session.get(Master, new_entry.master_id)
        business = await session.get(Business, appointment.business_id)
        client = await session.get(TelegramUser, client_id)
        location = (
            await session.get(Location, new_entry.location_id)
            if new_entry.location_id is not None
            else None
        )
        if master is None or business is None or client is None:
            raise AppointmentNotFoundError

        old_starts_at = appointment.service_starts_at
        old_ends_at = appointment.service_ends_at
        old_entry.state = CalendarEntryState.RELEASED.value
        new_entry.kind = CalendarEntryKind.APPOINTMENT.value
        hold.status = HoldStatus.CONVERTED.value
        appointment.calendar_entry_id = new_entry.id
        appointment.service_starts_at = hold.service_starts_at
        appointment.service_ends_at = hold.service_ends_at
        appointment.duration_minutes = int(
            (hold.service_ends_at - hold.service_starts_at).total_seconds() // 60
        )
        appointment.lock_version += 1

        await self._cancel_pending_notifications(
            session,
            appointment_id=appointment.id,
        )
        await self._schedule_client_reminders(
            session,
            appointment=appointment,
            client=client,
            master=master,
            business=business,
            now=now,
        )
        self._schedule_master_notification(
            session,
            appointment=appointment,
            master=master,
            business=business,
            kind="master_appointment_rescheduled_by_client",
            now=now,
        )
        session.add(
            AppointmentHistory(
                business_id=business.id,
                appointment_id=appointment.id,
                actor_user_id=client.id,
                event_type="rescheduled_by_client",
                from_status=appointment.status,
                to_status=appointment.status,
                event_payload={
                    "old_starts_at": old_starts_at.isoformat(),
                    "old_ends_at": old_ends_at.isoformat(),
                    "new_starts_at": appointment.service_starts_at.isoformat(),
                    "new_ends_at": appointment.service_ends_at.isoformat(),
                },
            )
        )
        await session.flush()
        return self._appointment_summary(
            appointment=appointment,
            master=master,
            business=business,
            location=location,
            now=now,
        )

    async def confirm_master_reschedule(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        master: Master,
        appointment_id: UUID,
        hold_id: UUID,
        holder_user_id: UUID,
        now: datetime | None = None,
    ) -> AppointmentSummary:
        now = now or datetime.now(UTC)
        appointment = await session.scalar(
            select(Appointment)
            .join(CalendarEntry, CalendarEntry.id == Appointment.calendar_entry_id)
            .where(
                Appointment.id == appointment_id,
                Appointment.business_id == business_id,
                CalendarEntry.master_id == master.id,
                Appointment.service_starts_at > now,
                Appointment.status.in_(
                    [
                        AppointmentStatus.PENDING_APPROVAL.value,
                        AppointmentStatus.CONFIRMED.value,
                    ]
                ),
            )
            .with_for_update()
        )
        if appointment is None:
            raise AppointmentChangeNotAllowedError
        hold = await session.scalar(
            select(SlotHold)
            .where(
                SlotHold.id == hold_id,
                SlotHold.business_id == business_id,
                SlotHold.client_id == holder_user_id,
            )
            .with_for_update()
        )
        if hold is None:
            raise HoldNotFoundError

        if hold.status != HoldStatus.ACTIVE.value:
            raise HoldExpiredError("The slot hold is no longer active")
        old_entry = await session.get(CalendarEntry, appointment.calendar_entry_id)
        new_entry = await session.get(CalendarEntry, hold.calendar_entry_id)
        if (
            hold.expires_at <= now
            or new_entry is None
            or new_entry.state != CalendarEntryState.ACTIVE.value
        ):
            if new_entry is not None and new_entry.state == CalendarEntryState.ACTIVE.value:
                new_entry.state = CalendarEntryState.EXPIRED.value
            hold.status = HoldStatus.EXPIRED.value
            await session.flush()
            raise HoldExpiredError("The slot hold has expired")
        if (
            old_entry is None
            or hold.service_id != appointment.service_id
            or new_entry.master_id != master.id
            or old_entry.master_id != master.id
        ):
            raise AppointmentChangeNotAllowedError("The hold does not match the appointment")

        business = await session.get(Business, business_id)
        client = await session.get(TelegramUser, appointment.client_id)
        location = (
            await session.get(Location, new_entry.location_id)
            if new_entry.location_id is not None
            else None
        )
        if business is None or client is None:
            raise AppointmentNotFoundError

        old_starts_at = appointment.service_starts_at
        old_ends_at = appointment.service_ends_at
        old_entry.state = CalendarEntryState.RELEASED.value
        new_entry.kind = CalendarEntryKind.APPOINTMENT.value
        hold.status = HoldStatus.CONVERTED.value
        appointment.calendar_entry_id = new_entry.id
        appointment.service_starts_at = hold.service_starts_at
        appointment.service_ends_at = hold.service_ends_at
        appointment.duration_minutes = int(
            (hold.service_ends_at - hold.service_starts_at).total_seconds() // 60
        )
        appointment.lock_version += 1

        await self._cancel_pending_notifications(session, appointment_id=appointment.id)
        await self._schedule_client_reminders(
            session,
            appointment=appointment,
            client=client,
            master=master,
            business=business,
            now=now,
        )
        if client.telegram_user_id is not None:
            session.add(
                NotificationJob(
                    business_id=business_id,
                    appointment_id=appointment.id,
                    recipient_user_id=client.id,
                    kind="client_appointment_rescheduled",
                    scheduled_for=now,
                    state=NotificationJobState.PENDING.value,
                )
            )
        session.add(
            AppointmentHistory(
                business_id=business_id,
                appointment_id=appointment.id,
                actor_user_id=holder_user_id,
                event_type="rescheduled_by_master",
                from_status=appointment.status,
                to_status=appointment.status,
                event_payload={
                    "old_starts_at": old_starts_at.isoformat(),
                    "old_ends_at": old_ends_at.isoformat(),
                    "new_starts_at": appointment.service_starts_at.isoformat(),
                    "new_ends_at": appointment.service_ends_at.isoformat(),
                },
            )
        )
        await session.flush()
        return self._appointment_summary(
            appointment=appointment,
            master=master,
            business=business,
            location=location,
            now=now,
        )

    async def _schedule_notifications(
        self,
        session: AsyncSession,
        *,
        appointment: Appointment,
        client: TelegramUser,
        master: Master,
        business: Business,
        now: datetime,
        notify_master: bool = True,
    ) -> None:
        await self._schedule_client_reminders(
            session,
            appointment=appointment,
            client=client,
            master=master,
            business=business,
            now=now,
        )
        if notify_master:
            self._schedule_master_notification(
                session,
                appointment=appointment,
                master=master,
                business=business,
                kind="master_new_appointment",
                now=now,
            )

    async def _schedule_client_reminders(
        self,
        session: AsyncSession,
        *,
        appointment: Appointment,
        client: TelegramUser,
        master: Master,
        business: Business,
        now: datetime,
    ) -> None:
        if client.telegram_user_id is None:
            return
        settings = await get_client_reminder_settings(
            session,
            business_id=business.id,
            master_user_id=master.user_id,
        )
        timezone = ZoneInfo(master.timezone or business.timezone)
        local_start = appointment.service_starts_at.astimezone(timezone)
        day_of = datetime.combine(
            local_start.date(),
            time(settings.day_of_hour),
            timezone,
        ).astimezone(UTC)
        reminders = [
            (
                "client_reminder_7d",
                appointment.service_starts_at - timedelta(days=7),
                settings.seven_days,
            ),
            (
                "client_reminder_3d",
                appointment.service_starts_at - timedelta(days=3),
                settings.three_days,
            ),
            ("client_reminder_day_of", day_of, settings.day_of),
        ]
        for kind, scheduled_for, enabled in reminders:
            if enabled and now < scheduled_for < appointment.service_starts_at:
                existing = await session.scalar(
                    select(NotificationJob).where(
                        NotificationJob.appointment_id == appointment.id,
                        NotificationJob.recipient_user_id == client.id,
                        NotificationJob.kind == kind,
                        NotificationJob.scheduled_for == scheduled_for,
                    )
                )
                if existing is None:
                    session.add(
                        NotificationJob(
                            business_id=business.id,
                            appointment_id=appointment.id,
                            recipient_user_id=client.id,
                            kind=kind,
                            scheduled_for=scheduled_for,
                            state=NotificationJobState.PENDING.value,
                        )
                    )
                else:
                    existing.state = NotificationJobState.PENDING.value
                    existing.last_error = None

    async def schedule_client_reminders_for_appointments(
        self,
        session: AsyncSession,
        *,
        appointment_ids: list[UUID],
        now: datetime | None = None,
    ) -> None:
        if not appointment_ids:
            return
        now = now or datetime.now(UTC)
        rows = (
            await session.execute(
                select(Appointment, CalendarEntry, Master, Business, TelegramUser)
                .join(CalendarEntry, CalendarEntry.id == Appointment.calendar_entry_id)
                .join(Master, Master.id == CalendarEntry.master_id)
                .join(Business, Business.id == Appointment.business_id)
                .join(TelegramUser, TelegramUser.id == Appointment.client_id)
                .where(
                    Appointment.id.in_(appointment_ids),
                    Appointment.service_starts_at > now,
                    Appointment.status.in_(
                        [
                            AppointmentStatus.PENDING_APPROVAL.value,
                            AppointmentStatus.PENDING_PAYMENT.value,
                            AppointmentStatus.CONFIRMED.value,
                        ]
                    ),
                )
            )
        ).all()
        for appointment, _entry, master, business, client in rows:
            await self._schedule_client_reminders(
                session,
                appointment=appointment,
                client=client,
                master=master,
                business=business,
                now=now,
            )
        await session.flush()

    async def rebuild_client_reminders_for_master(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        master: Master,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(UTC)
        appointment_ids = list(
            (
                await session.scalars(
                    select(Appointment.id)
                    .join(CalendarEntry, CalendarEntry.id == Appointment.calendar_entry_id)
                    .where(
                        Appointment.business_id == business_id,
                        CalendarEntry.master_id == master.id,
                        Appointment.service_starts_at > now,
                        Appointment.status.in_(
                            [
                                AppointmentStatus.PENDING_APPROVAL.value,
                                AppointmentStatus.PENDING_PAYMENT.value,
                                AppointmentStatus.CONFIRMED.value,
                            ]
                        ),
                    )
                )
            ).all()
        )
        if not appointment_ids:
            return
        await session.execute(
            update(NotificationJob)
            .where(
                NotificationJob.appointment_id.in_(appointment_ids),
                NotificationJob.kind.startswith("client_reminder_"),
                NotificationJob.state == NotificationJobState.PENDING.value,
            )
            .values(state=NotificationJobState.CANCELLED.value)
        )
        await self.schedule_client_reminders_for_appointments(
            session,
            appointment_ids=appointment_ids,
            now=now,
        )

    @staticmethod
    def _schedule_master_notification(
        session: AsyncSession,
        *,
        appointment: Appointment,
        master: Master,
        business: Business,
        kind: str,
        now: datetime,
    ) -> None:
        if master.user_id is not None:
            session.add(
                NotificationJob(
                    business_id=business.id,
                    appointment_id=appointment.id,
                    recipient_user_id=master.user_id,
                    kind=kind,
                    scheduled_for=now,
                    state=NotificationJobState.PENDING.value,
                )
            )

    @staticmethod
    async def _cancel_pending_notifications(
        session: AsyncSession,
        *,
        appointment_id: UUID,
    ) -> None:
        await session.execute(
            update(NotificationJob)
            .where(
                NotificationJob.appointment_id == appointment_id,
                NotificationJob.state == NotificationJobState.PENDING.value,
            )
            .values(state=NotificationJobState.CANCELLED.value)
        )

    async def _get_changeable_appointment(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        client_id: UUID,
        appointment_id: UUID,
        now: datetime,
    ) -> Appointment:
        appointment = await session.scalar(
            select(Appointment)
            .where(
                Appointment.id == appointment_id,
                Appointment.business_id == business_id,
                Appointment.client_id == client_id,
            )
            .with_for_update()
        )
        if appointment is None:
            raise AppointmentNotFoundError
        if not self._can_change(appointment, now):
            raise AppointmentChangeNotAllowedError
        return appointment

    def _appointment_summary(
        self,
        *,
        appointment: Appointment,
        master: Master,
        business: Business,
        location: Location | None,
        now: datetime,
    ) -> AppointmentSummary:
        timezone = ZoneInfo(master.timezone or business.timezone)
        local_start = appointment.service_starts_at.astimezone(timezone)
        return AppointmentSummary(
            appointment_id=appointment.id,
            booking_request_id=appointment.booking_request_id,
            service_id=appointment.service_id,
            service_name=appointment.service_name_snapshot,
            master_name=master.display_name,
            location_name=location.name if location else None,
            location_address=location.address if location else None,
            local_start=local_start,
            local_end=appointment.service_ends_at.astimezone(timezone),
            status=appointment.status,
            can_change=self._can_change(appointment, now),
            change_deadline=local_start - timedelta(hours=self._settings.cancellation_cutoff_hours),
        )

    def _can_change(self, appointment: Appointment, now: datetime) -> bool:
        return appointment.status in {
            AppointmentStatus.PENDING_APPROVAL.value,
            AppointmentStatus.PENDING_PAYMENT.value,
            AppointmentStatus.CONFIRMED.value,
        } and appointment.service_starts_at - now >= timedelta(
            hours=self._settings.cancellation_cutoff_hours
        )

    @staticmethod
    def _find_slot(
        slots: list[BookableSlot],
        service_start: datetime,
    ) -> BookableSlot | None:
        return next(
            (slot for slot in slots if slot.service_start == service_start),
            None,
        )

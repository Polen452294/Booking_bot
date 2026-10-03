from aiogram.fsm.state import State, StatesGroup


class BookingStates(StatesGroup):
    selecting_service = State()
    choosing_pricing_flow = State()
    selecting_date = State()
    selecting_slot = State()
    waiting_phone = State()
    confirming = State()
    rescheduling_date = State()
    rescheduling_slot = State()
    rescheduling_confirming = State()


class RequestStates(StatesGroup):
    collecting = State()
    phone = State()
    reply = State()
    price = State()
    comment = State()
    proposal_confirm = State()


class MasterStates(StatesGroup):
    waiting_weekday_hours = State()
    waiting_time_block = State()
    waiting_extra_day = State()
    waiting_service_create_name = State()
    waiting_service_name = State()
    waiting_service_description = State()
    waiting_service_duration = State()
    waiting_service_price = State()
    waiting_service_buffers = State()
    manual_selecting_service = State()
    manual_selecting_date = State()
    manual_selecting_slot = State()
    manual_waiting_name = State()
    manual_waiting_phone = State()
    manual_waiting_comment = State()
    manual_confirming = State()
    waiting_appointment_duration = State()
    waiting_internal_note = State()
    rescheduling_date = State()
    rescheduling_slot = State()
    rescheduling_confirming = State()
    waiting_reminder_hour = State()


class SpecialistSetupStates(StatesGroup):
    waiting_name = State()
    waiting_bio = State()
    selecting_days = State()
    waiting_day_hours = State()
    confirming = State()

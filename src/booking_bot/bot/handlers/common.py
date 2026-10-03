from html import escape
from uuid import UUID

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from booking_bot.bot.commands import set_master_commands
from booking_bot.bot.handlers.specialist_setup import begin_specialist_onboarding
from booking_bot.bot.keyboards import main_menu_keyboard
from booking_bot.config import get_settings
from booking_bot.services.bookings import BookingService
from booking_bot.services.master_access import (
    InvalidMasterInviteError,
    MasterAlreadyLinkedError,
    get_master_for_user,
    redeem_master_invite,
)
from booking_bot.services.specialist_context import get_specialist_context
from booking_bot.services.users import get_or_create_telegram_user
from booking_bot.specialist_config import get_specialist_template

router = Router(name="common")


@router.message(Command("cancel"))
async def cancel_input(message: Message, state: FSMContext, db_session: AsyncSession) -> None:
    from aiogram.types import ReplyKeyboardRemove

    data = await state.get_data()
    if data.get("hold_id") and message.from_user:
        user = await get_or_create_telegram_user(db_session, message.from_user)
        await BookingService(get_settings()).release_hold(
            db_session,
            hold_id=UUID(data["hold_id"]),
            client_id=user.id,
        )
    await state.clear()
    await message.answer("Ввод отменён.", reply_markup=ReplyKeyboardRemove())
    await message.answer("Главное меню", reply_markup=main_menu_keyboard())


@router.message(CommandStart())
async def start_handler(
    message: Message,
    business_id: UUID,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if message.from_user is None:
        return
    previous_state = await state.get_state()
    data = await state.get_data()
    try:
        if "hold_id" in data and "client_id" in data:
            await BookingService(get_settings()).release_hold(
                db_session,
                hold_id=UUID(data["hold_id"]),
                client_id=UUID(data["client_id"]),
            )
    except ValueError:
        pass
    await state.clear()
    from aiogram.types import ReplyKeyboardRemove

    if previous_state is not None:
        await message.answer("Ввод завершён.", reply_markup=ReplyKeyboardRemove())
    user = await get_or_create_telegram_user(db_session, message.from_user)
    payload = ""
    if message.text:
        command_parts = message.text.split(maxsplit=1)
        payload = command_parts[1].strip() if len(command_parts) == 2 else ""
    if payload.startswith("master_"):
        try:
            master = await redeem_master_invite(
                db_session,
                business_id=business_id,
                token=payload.removeprefix("master_"),
                user=user,
            )
        except InvalidMasterInviteError:
            await message.answer(
                "Ссылка приглашения недействительна или уже использована.",
                reply_markup=main_menu_keyboard(),
            )
            return
        except MasterAlreadyLinkedError:
            await message.answer(
                "Этот профиль специалиста уже привязан к другому пользователю.",
                reply_markup=main_menu_keyboard(),
            )
            return
        await set_master_commands(message.bot, chat_id=message.chat.id)
        await begin_specialist_onboarding(
            message,
            state,
            db_session,
            business_id=business_id,
            master=master,
        )
        return

    actor_master = await get_master_for_user(
        db_session,
        business_id=business_id,
        user_id=user.id,
    )
    specialist = (await get_specialist_context(db_session)).master
    await message.answer(
        get_specialist_template().text(
            "welcome",
            "Здравствуйте! Здесь можно выбрать услугу, дату и свободное время.",
            specialist_name=escape(specialist.display_name),
            specialist_bio=escape(specialist.bio or ""),
        ),
        reply_markup=main_menu_keyboard(
            master_access=actor_master is not None,
            unread_count=await _unread_count(db_session, business_id, user.id),
        ),
    )


async def _unread_count(session, business_id, actor_user_id):
    from booking_bot.services.conversations import ConversationService

    return await ConversationService().total_unread(
        session,
        business_id=business_id,
        actor_user_id=actor_user_id,
    )


@router.message(Command("help"))
async def help_handler(message: Message) -> None:
    await message.answer(
        get_specialist_template().text(
            "help",
            "Используйте /start, чтобы открыть меню. "
            "Выбранное время удерживается 10 минут до подтверждения.",
        ),
        reply_markup=main_menu_keyboard(),
    )

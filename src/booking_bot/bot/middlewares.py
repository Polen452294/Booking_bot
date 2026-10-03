from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from booking_bot.bot.transactional_fsm import BufferedFSMContext
from booking_bot.db.session import async_session_factory


async def persist_before_response(session: AsyncSession, state: FSMContext) -> None:
    """Transport boundary: persist the complete operation before showing success.

    The existing webhook receipt commits in the same transaction. A failed UI
    delivery after this point cannot roll back or replay the business operation.
    """
    await session.commit()
    if isinstance(state, BufferedFSMContext):
        await state.apply()


class DatabaseSessionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if data.get("db_session") is not None:
            commit_update = data.get("commit_update")
            if commit_update is None:
                return await handler(event, data)
            original_state = data.get("state")
            buffered = BufferedFSMContext(original_state) if original_state is not None else None
            if buffered is not None:
                await buffered.load(data.get("raw_state"))
                data["state"] = buffered
            result = await handler(event, data)
            # The webhook owns the transaction and supplies its commit callback.
            # This runs inside aiogram's FSM isolation lock, before exposing FSM writes.
            await commit_update()
            if buffered is not None:
                await buffered.apply()
            return result

        async with async_session_factory() as session:
            data["db_session"] = session
            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            else:
                await session.commit()
                return result

import asyncio
import hmac
import logging
from typing import Any

from aiogram.types import Update
from aiogram.types.update import UpdateTypeLookupError
from pydantic import Field, StrictInt
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from booking_bot.bot import dispatcher
from booking_bot.bot.factory import create_telegram_bot
from booking_bot.config import Settings
from booking_bot.services.specialist_context import (
    SpecialistNotConfiguredError,
    get_specialist_context,
)
from booking_bot.services.update_idempotency import (
    PROCESSING_TIMEOUT,
    UpdateLease,
    claim_receipt,
)

logger = logging.getLogger(__name__)


class WebhookUpdate(Update):
    update_id: StrictInt = Field(ge=0, le=2**63 - 1)


class InvalidWebhookSecretError(PermissionError):
    pass


class TelegramBotNotConfiguredError(RuntimeError):
    pass


class TelegramWebhookService:
    """Deliver updates to the only Telegram bot configured for this deployment."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def process(
        self,
        *,
        webhook_header_secret: str | None,
        payload: dict[str, Any],
        session: AsyncSession,
    ) -> None:
        expected_secret = (
            self._settings.telegram_webhook_header_secret.get_secret_value()
            if self._settings.telegram_webhook_header_secret
            else None
        )
        token = (
            self._settings.telegram_bot_token.get_secret_value()
            if self._settings.telegram_bot_token
            else None
        )
        if not expected_secret or not token:
            raise TelegramBotNotConfiguredError
        if webhook_header_secret is None or not hmac.compare_digest(
            webhook_header_secret.encode(),
            expected_secret.encode(),
        ):
            raise InvalidWebhookSecretError

        update = WebhookUpdate.model_validate(payload)
        lease = UpdateLease(
            dispatcher.storage.redis, self._settings.redis_namespace, update.update_id
        )
        if not await lease.acquire():
            return
        bot = create_telegram_bot(token, self._settings)
        try:
            async with asyncio.timeout(PROCESSING_TIMEOUT):
                if await claim_receipt(session, self._settings.redis_namespace, update.update_id):
                    try:
                        context = await get_specialist_context(session)
                    except SpecialistNotConfiguredError as exc:
                        raise TelegramBotNotConfiguredError from exc
                    try:
                        _ = update.event_type
                    except UpdateTypeLookupError:
                        # Aiogram's fallback warning includes the whole payload; avoid PII.
                        logger.info(
                            "Unknown Telegram update ignored update_id=%s", update.update_id
                        )
                        await session.commit()
                    else:
                        await dispatcher.feed_update(
                            bot,
                            update,
                            business_id=context.business_id,
                            specialist_master_id=context.master_id,
                            db_session=session,
                            commit_update=session.commit,
                        )
                else:
                    await session.commit()
            await lease.finish()
        except (Exception, asyncio.CancelledError):
            await session.rollback()
            try:
                await lease.release()
            except RedisError:
                logger.warning("Webhook lease release unavailable; waiting for expiry")
            raise
        finally:
            await bot.session.close()

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from booking_bot.config import Settings


def create_telegram_bot(token: str, settings: Settings) -> Bot:
    proxy_url = (
        settings.telegram_proxy_url.get_secret_value() if settings.telegram_proxy_url else None
    )
    session = AiohttpSession(proxy=proxy_url, timeout=20)
    return Bot(
        token=token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

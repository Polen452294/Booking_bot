import argparse
import asyncio
import logging
import re

from aiogram import Bot

from booking_bot.bot import dispatcher
from booking_bot.bot.commands import set_client_commands, set_master_commands
from booking_bot.bot.factory import create_telegram_bot
from booking_bot.config import Settings, get_settings
from booking_bot.db.models import TelegramUser
from booking_bot.db.session import async_session_factory, engine
from booking_bot.lifecycle import shutdown_event
from booking_bot.logging_config import SafeFormatter, configure_logging
from booking_bot.services.master_access import create_master_invite
from booking_bot.services.notification_delivery import NotificationDeliveryService
from booking_bot.services.specialist_context import get_specialist_context
from booking_bot.services.specialist_setup import configure_specialist
from booking_bot.services.worker_health import (
    WorkerHeartbeat,
    check_worker_health,
    create_health_redis,
)
from booking_bot.specialist_config import (
    get_specialist_template,
    load_specialist_template,
)

logger = logging.getLogger(__name__)


async def configure_copy(args: argparse.Namespace) -> None:
    template = load_specialist_template(args.config) if args.config else get_specialist_template()
    async with async_session_factory() as session:
        deployment = await configure_specialist(
            session,
            template,
            replace_schedule=args.reset_schedule,
        )
        await session.commit()
    logger.info("Specialist configured: %s (profile %s)", template.profile.slug, deployment.id)


async def run_polling(_: argparse.Namespace, stop: asyncio.Event | None = None) -> None:
    settings = get_settings()
    if settings.is_production:
        raise RuntimeError("Polling is only available in development; use the production webhook")
    if settings.telegram_bot_token is None:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    async with async_session_factory() as session:
        context = await get_specialist_context(session)
        master_user = (
            await session.get(TelegramUser, context.master.user_id)
            if context.master.user_id is not None
            else None
        )

    bot = create_telegram_bot(settings.telegram_bot_token.get_secret_value(), settings)
    try:
        await bot.get_me()
        await bot.delete_webhook(drop_pending_updates=False)
        await set_client_commands(bot)
        if master_user is not None and master_user.telegram_user_id is not None:
            await set_master_commands(bot, chat_id=master_user.telegram_user_id)
        if stop is not None and stop.is_set():
            return
        logger.info("Polling starting")

        async def stop_polling() -> None:
            await stop.wait()
            await dispatcher.stop_polling()

        watcher = asyncio.create_task(stop_polling()) if stop is not None else None
        try:
            await dispatcher.start_polling(
                bot,
                business_id=context.business_id,
                specialist_master_id=context.master_id,
                close_bot_session=False,
                handle_signals=stop is None,
            )
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            # Aiogram dispatches updates as tasks. Drain them before closing DB / Telegram.
            pending = list(dispatcher._handle_update_tasks)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await bot.session.close()


async def create_master_invite_link(args: argparse.Namespace) -> None:
    settings = get_settings()
    if settings.telegram_bot_token is None:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    async with async_session_factory() as session:
        context = await get_specialist_context(session)
        invite = await create_master_invite(
            session,
            business_id=context.business_id,
            master_id=context.master_id,
        )
        await session.commit()

    username = getattr(args, "bot_username", None)
    if username is None:
        bot = create_telegram_bot(settings.telegram_bot_token.get_secret_value(), settings)
        try:
            username = (await bot.get_me()).username
        finally:
            await bot.session.close()
    print(f"Specialist: {invite.master_name}")
    print(f"Invite expires at: {invite.expires_at.isoformat()}")
    print(f"Invite link: https://t.me/{username}?start=master_{invite.token}")


async def run_notification_worker(
    args: argparse.Namespace,
    stop: asyncio.Event | None = None,
) -> None:
    settings = get_settings()
    if settings.telegram_bot_token is None:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    async with async_session_factory() as session:
        context = await get_specialist_context(session)

    bot = create_telegram_bot(settings.telegram_bot_token.get_secret_value(), settings)
    redis = create_health_redis(settings)
    heartbeat = WorkerHeartbeat(redis, settings)
    try:
        service = NotificationDeliveryService(settings, heartbeat=heartbeat)
        logger.info("Notification worker started")
        if args.once:
            processed = await service.run_once(bot, business_id=context.business_id, stop=stop)
            logger.info("Processed jobs: %s", processed)
        else:
            await service.run_forever(bot, business_id=context.business_id, stop=stop)
    finally:
        try:
            await bot.session.close()
        finally:
            try:
                await heartbeat.clear()
            except Exception:
                logger.warning("Worker heartbeat cleanup unavailable; waiting for expiry")
            finally:
                await redis.aclose()
        logger.info("Notification worker shutdown complete")


async def report_webhook(bot: Bot, settings: Settings) -> bool:
    info = await bot.get_webhook_info()
    identity = await bot.get_me()
    expected = f"{str(settings.telegram_webhook_base_url).rstrip('/')}/api/v1/webhooks/telegram"
    redact = SafeFormatter(settings, "webhook-status").redact

    # URLs are useful here, but must never reveal the bot token or secret.
    def safe_url(url: str) -> str:
        for secret in (settings.telegram_bot_token, settings.telegram_webhook_header_secret):
            if secret:
                url = url.replace(secret.get_secret_value(), "[REDACTED]")
        return url

    print(f"Bot: @{identity.username}")
    print(f"Expected URL: {safe_url(expected)}")
    print(f"Telegram URL: {safe_url(info.url) or 'not configured'}")
    print(f"Pending updates: {info.pending_update_count}")
    print(f"Last error: {redact(info.last_error_message) if info.last_error_message else 'none'}")
    if info.last_error_date:
        print(f"Last error date: {info.last_error_date.isoformat()}")
    print(f"Max connections: {info.max_connections}")
    print(f"Allowed updates: {', '.join(info.allowed_updates or []) or 'default'}")
    if info.url != expected:
        print("Status: ERROR (Telegram URL differs from configured URL)")
        return False
    if info.last_error_message or info.pending_update_count:
        print("Status: WARNING (pending updates or historical Telegram delivery error)")
    else:
        print("Status: OK")
    return True


async def webhook_status(_: argparse.Namespace) -> None:
    settings = get_settings()
    if settings.telegram_bot_token is None or settings.telegram_webhook_base_url is None:
        raise RuntimeError("Telegram token and webhook URL must be configured")
    bot = create_telegram_bot(settings.telegram_bot_token.get_secret_value(), settings)
    try:
        if not await report_webhook(bot, settings):
            raise SystemExit(1)
    finally:
        await bot.session.close()


async def set_webhook(_: argparse.Namespace) -> None:
    settings = get_settings()
    if settings.telegram_webhook_mode == "internal":
        raise RuntimeError("Webhook registration is disabled in internal mode")
    if settings.telegram_bot_token is None:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    if settings.telegram_webhook_base_url is None:
        raise RuntimeError("TELEGRAM_WEBHOOK_BASE_URL is not configured")
    if settings.telegram_webhook_header_secret is None:
        raise RuntimeError("TELEGRAM_WEBHOOK_HEADER_SECRET is not configured")

    url = f"{str(settings.telegram_webhook_base_url).rstrip('/')}/api/v1/webhooks/telegram"
    bot = create_telegram_bot(settings.telegram_bot_token.get_secret_value(), settings)
    try:
        configured = await bot.set_webhook(
            url=url,
            secret_token=settings.telegram_webhook_header_secret.get_secret_value(),
            drop_pending_updates=False,
        )
        if not configured:
            raise RuntimeError("Telegram did not confirm webhook configuration")
        print("Webhook configured")
        if not await report_webhook(bot, settings):
            raise SystemExit(1)
    finally:
        await bot.session.close()


async def async_main(args: argparse.Namespace) -> None:
    try:
        if args.command == "configure":
            await configure_copy(args)
        elif args.command == "run-polling":
            with shutdown_event() as stop:
                await run_polling(args, stop)
        elif args.command == "create-master-invite":
            await create_master_invite_link(args)
        elif args.command == "run-worker":
            with shutdown_event() as stop:
                await run_notification_worker(args, stop)
        elif args.command == "set-webhook":
            await set_webhook(args)
        elif args.command == "webhook-status":
            await webhook_status(args)
        elif args.command == "worker-health":
            if not await check_worker_health(get_settings(), args.worker_id):
                raise SystemExit(1)
    finally:
        try:
            await dispatcher.fsm.close()
        finally:
            await engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="booking-admin")
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure = subparsers.add_parser(
        "configure",
        help="Apply specialist.toml to this bot copy",
    )
    configure.add_argument("--config", help="Alternative TOML config path")
    configure.add_argument(
        "--reset-schedule",
        action="store_true",
        help="Replace working hours with the schedule from TOML",
    )
    subparsers.add_parser(
        "run-polling",
        help="Run this specialist bot locally without a webhook",
    )
    invite = subparsers.add_parser(
        "create-master-invite",
        help="Create a one-time owner link for this specialist",
    )
    invite.add_argument(
        "--bot-username",
        type=validated_bot_username,
        help="Username already verified by bookingctl getMe (avoids a second Telegram call)",
    )
    worker = subparsers.add_parser(
        "run-worker",
        help="Deliver due Telegram notification jobs",
    )
    worker.add_argument("--once", action="store_true")
    subparsers.add_parser(
        "set-webhook",
        help="Configure the single production webhook",
    )
    subparsers.add_parser("webhook-status", help="Inspect Telegram webhook configuration")
    health = subparsers.add_parser(
        "worker-health", help="Check PostgreSQL, Redis and worker progress"
    )
    health.add_argument("--worker-id", help="Worker hostname (defaults to this container/host)")
    return parser


def validated_bot_username(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", value):
        raise argparse.ArgumentTypeError("Invalid bot username")
    return value


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()
    configure_logging(settings, config_path=args.config if args.command == "configure" else None)
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Administrative command failed")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

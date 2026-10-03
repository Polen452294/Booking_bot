"""Production ASGI entry point with one worker and the shared logging configuration."""

import uvicorn

from booking_bot.config import get_settings
from booking_bot.logging_config import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    uvicorn.run(
        "booking_bot.main:app",
        host="0.0.0.0",
        port=8000,
        log_config=None,
        access_log=not settings.is_production,
        timeout_graceful_shutdown=60,
        workers=1,
    )


if __name__ == "__main__":
    main()

"""Small, dependency-free log formatters; never serialize request or exception locals."""

import json
import logging
import os
import re
import traceback
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from booking_bot.config import Settings
from booking_bot.specialist_config import load_specialist_template


class SafeFormatter(logging.Formatter):
    def __init__(self, settings: Settings, slug: str) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self.production = settings.is_production
        self.slug = slug
        service = os.environ.get("LOG_SERVICE", "application")
        self.service = service if service in {"api", "worker", "admin"} else "application"
        secrets = []
        for name in (
            "POSTGRES_PASSWORD",
            "REDIS_PASSWORD",
            "MONITORING_TELEGRAM_BOT_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_ACCESS_KEY_ID",
        ):
            if os.environ.get(name):
                secrets.append(os.environ[name])
        for value in (
            settings.telegram_bot_token,
            settings.telegram_webhook_header_secret,
            settings.telegram_proxy_url,
        ):
            if value and value.get_secret_value():
                secrets.append(value.get_secret_value())
        for url in (settings.database_url, settings.redis_url):
            secrets.append(url)
            password = urlsplit(url).password
            if password:
                secrets.extend((password, unquote(password)))
        self.secrets = sorted(set(secrets), key=len, reverse=True)

    def redact(self, message: str) -> str:
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(r"\b\d{5,16}:[A-Za-z0-9_-]{20,}", "[REDACTED_TOKEN]", message)
        message = re.sub(r"\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", "[REDACTED_URL]", message)
        message = re.sub(r"(?<!\w)\+?\d[\d ()-]{9,}\d(?!\w)", "[REDACTED_PHONE]", message)
        return re.sub(
            r"(?i)\b(authorization|x-telegram-bot-api-secret-token|"
            r"postgres_password|redis_password|aws_secret_access_key|aws_session_token|"
            r"monitoring_telegram_bot_token)\b[\"']?\s*[:=]\s*[^\r\n]+",
            r"\1=[REDACTED]",
            message,
        )

    def format(self, record: logging.LogRecord) -> str:
        if not self.production:
            return self.redact(super().format(record))
        message = record.getMessage()
        event = str(record.msg)
        # Library exception messages may embed Telegram updates or SQL parameters.
        if record.exc_info and not record.name.startswith("booking_bot"):
            message = "Unhandled library exception"
            event = message
        result = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": self.redact(message),
            "specialist_slug": self.slug,
            "deployment_slug": self.slug,
            "service": self.service,
            "event": self.redact(event),
        }
        for field in ("job_id", "appointment_id", "update_id"):
            value = getattr(record, field, None)
            if value is not None:
                result[field] = self.redact(str(value))
        if record.exc_info:
            error_type, _, tb = record.exc_info
            result["exception_type"] = error_type.__name__ if error_type else "Unknown"
            # Keep locations useful for diagnosis, without values, source text or locals.
            result["traceback"] = [
                f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"
                for frame in traceback.extract_tb(tb)
            ]
        return json.dumps(result, ensure_ascii=False)


def configure_logging(settings: Settings, *, config_path: str | None = None) -> None:
    template = load_specialist_template(config_path or settings.specialist_config_path)
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter(settings, template.profile.slug))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    if settings.is_production:
        # Request URLs may contain PII; SQL/HTTP debug payloads are not production events.
        for name in ("uvicorn.access", "sqlalchemy.engine", "httpx", "httpcore", "aiohttp"):
            logging.getLogger(name).setLevel(logging.WARNING)

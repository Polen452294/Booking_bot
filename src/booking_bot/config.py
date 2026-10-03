import re
from functools import lru_cache
from typing import Literal, Self
from urllib.parse import unquote, urlsplit

from pydantic import AnyHttpUrl, Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


def unsafe_secret(value: str, *, minimum: int = 24) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    return (
        len(value) < minimum
        or len(set(value)) < 6
        or any(
            value == (value[:size] * (len(value) // size + 1))[: len(value)]
            for size in range(1, min(16, len(value) // 2) + 1)
        )
        or any(
            word in normalized
            for word in (
                "changeme",
                "replaceme",
                "yourpassword",
                "yoursecret",
                "example",
                "password",
                "testsecret",
                "supersecret",
                "development",
            )
        )
        or normalized in {"booking", "redis", "postgres", "secret", "default", "admin"}
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    app_name: str = "Telegram Specialist Booking Bot"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = Field(
        default="postgresql+asyncpg://booking:booking@localhost:55432/booking", repr=False
    )
    redis_url: str = Field(default="redis://localhost:6379/0", repr=False)
    db_pool_size: int = Field(default=3, ge=1, le=20)
    db_max_overflow: int = Field(default=2, ge=0, le=20)
    db_pool_timeout: float = Field(default=5, gt=0, le=60)
    db_pool_recycle: int = Field(default=1800, ge=60)
    telegram_webhook_base_url: AnyHttpUrl | None = Field(default=None, repr=False)
    telegram_webhook_mode: Literal["public", "internal"] = "public"
    telegram_bot_token: SecretStr | None = None
    telegram_proxy_url: SecretStr | None = None
    telegram_webhook_header_secret: SecretStr | None = None
    specialist_config_path: str = "specialist.toml"
    booking_horizon_days: int = 60
    booking_min_lead_hours: int = 3
    slot_hold_minutes: int = 10
    cancellation_cutoff_hours: int = 24
    booking_dates_shown: int = 14
    notification_poll_interval_seconds: float = Field(default=2.0, gt=0)
    notification_batch_size: int = Field(default=20, ge=1, le=100)
    notification_max_attempts: int = Field(default=5, ge=1)

    @property
    def redis_namespace(self) -> str:
        # A bot identifies this single-specialist deployment; never include its secret.
        bot_id = (
            self.telegram_bot_token.get_secret_value().split(":", 1)[0]
            if self.telegram_bot_token else "unconfigured"
        )
        return f"booking:{bot_id}"

    @field_validator("telegram_webhook_base_url", mode="before")
    @classmethod
    def empty_webhook_url(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def validate_production(self) -> Self:
        if not self.is_production:
            return self
        required = (
            "telegram_bot_token",
            "telegram_webhook_header_secret",
            "database_url",
            "redis_url",
            "specialist_config_path",
        )
        for name in required:
            value = getattr(self, name)
            if name not in self.model_fields_set or value is None or not str(value).strip():
                raise ValueError(f"{name.upper()} is required in production")
        if self.telegram_webhook_mode == "public" and self.telegram_webhook_base_url is None:
            raise ValueError("TELEGRAM_WEBHOOK_BASE_URL is required in production public mode")
        token = self.telegram_bot_token.get_secret_value() if self.telegram_bot_token else ""
        if not re.fullmatch(r"[0-9]{5,16}:[A-Za-z0-9_-]{30,128}", token):
            raise ValueError("TELEGRAM_BOT_TOKEN has an invalid format")
        webhook = self.telegram_webhook_base_url
        if webhook is not None and (
            webhook.scheme != "https"
            or webhook.username
            or webhook.password
            or webhook.query
            or webhook.fragment
        ):
            raise ValueError("TELEGRAM_WEBHOOK_BASE_URL must be HTTPS without credentials or query")
        secret = (
            self.telegram_webhook_header_secret.get_secret_value()
            if self.telegram_webhook_header_secret
            else ""
        )
        if unsafe_secret(secret, minimum=32) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", secret):
            raise ValueError("TELEGRAM_WEBHOOK_HEADER_SECRET must be a strong URL-safe secret")
        try:
            database = make_url(self.database_url)
            redis = urlsplit(self.redis_url)
            valid_database = (
                database.drivername == "postgresql+asyncpg"
                and database.host
                and database.database
                and database.username
                and database.password
                and not unsafe_secret(database.password)
                and database.password != database.username
            )
            valid_redis = (
                redis.scheme in {"redis", "rediss"}
                and redis.hostname
                and redis.password
                and not unsafe_secret(unquote(redis.password))
                and redis.path.removeprefix("/").isdigit()
                and not redis.query
                and not redis.fragment
                and (redis.port is None or 0 < redis.port < 65536)
            )
        except (ValueError, TypeError, ArgumentError):
            raise ValueError("Invalid production database or Redis configuration") from None
        if not valid_database:
            raise ValueError("DATABASE_URL requires PostgreSQL and a strong non-default password")
        if not valid_redis:
            raise ValueError("REDIS_URL requires Redis, a database number and a strong password")
        from booking_bot.specialist_config import SpecialistConfigError, load_specialist_template

        try:
            load_specialist_template(self.specialist_config_path)
        except SpecialistConfigError:
            raise ValueError(
                "SPECIALIST_CONFIG_PATH must point to a valid specialist config"
            ) from None
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        # Do not let startup tracebacks / CLI pretty-printers display raw environment inputs.
        errors = exc.errors(include_input=False, include_context=False)
        detail = "; ".join(
            f"{'.'.join(map(str, error['loc'])) or 'settings'}: {error['msg']}" for error in errors
        )
        raise RuntimeError("Invalid application configuration: " + detail) from None

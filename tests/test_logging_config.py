import json
import logging
import subprocess
import sys
from pathlib import Path

from booking_bot.config import Settings
from booking_bot.logging_config import SafeFormatter


def test_production_logs_hide_secrets_and_exception_payloads() -> None:
    settings = Settings(
        _env_file=None,
        telegram_bot_token="123456789:example_token_value_that_must_be_hidden",
        telegram_webhook_header_secret="header-secret-value",
        database_url="postgresql+asyncpg://owner:db-secret-value@host/booking",
        redis_url="redis://:redis-secret-value@host/0",
    )
    # Formatter unit test; production settings validation is covered separately.
    settings = settings.model_copy(update={"app_env": "production"})
    formatter = SafeFormatter(settings, "test-specialist")
    try:
        raise RuntimeError("client phone +79991234567, name Alice, secret exception body")
    except RuntimeError:
        record = logging.LogRecord(
            "booking_bot.http",
            logging.ERROR,
            __file__,
            12,
            "Failure: %s %s %s %s Authorization: Bearer private-bearer-value",
            (
                settings.telegram_bot_token.get_secret_value(),
                settings.telegram_webhook_header_secret.get_secret_value(),
                settings.database_url,
                settings.redis_url,
            ),
            sys.exc_info(),
        )
    rendered = formatter.format(record)
    for private in (
        "header-secret-value",
        "db-secret-value",
        "redis-secret-value",
        "private-bearer-value",
        "example_token_value",
        "+79991234567",
        "Alice",
        "secret exception body",
    ):
        assert private not in rendered
    event = json.loads(rendered)
    assert event["specialist_slug"] == "test-specialist"
    assert event["exception_type"] == "RuntimeError"
    assert event["level"] == "ERROR"
    assert event["traceback"]
    assert event["timestamp"]


def test_development_logging_is_readable_and_redacted() -> None:
    formatter = SafeFormatter(Settings(_env_file=None), "test")
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "Connection redis://user:unknown-password@host/0",
        (),
        None,
    )
    rendered = formatter.format(record)
    assert "INFO test Connection" in rendered
    assert "unknown-password" not in rendered


def test_logging_supports_configure_with_alternative_toml(tmp_path) -> None:
    template = Path(__file__).resolve().parents[1] / "specialist.toml"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from booking_bot.config import Settings; "
                "from booking_bot.logging_config import configure_logging; "
                "configure_logging(Settings(_env_file=None), config_path=sys.argv[1])"
            ),
            str(template),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr

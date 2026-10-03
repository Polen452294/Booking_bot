import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from booking_bot.config import Settings
from booking_bot.specialist_config import SpecialistConfigError, load_specialist_template

ROOT = Path(__file__).resolve().parents[1]
TEST_SECRET = "9bd16385e74a026fc085217abe40963d"


@pytest.fixture
def production_values(monkeypatch) -> dict:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    return {
        "app_env": "production",
        "telegram_bot_token": f"123456789:{TEST_SECRET}abcd",
        "telegram_webhook_base_url": "https://booking.example.org",
        "telegram_webhook_header_secret": TEST_SECRET,
        "database_url": f"postgresql+asyncpg://owner:{TEST_SECRET}@postgres/booking",
        "redis_url": f"redis://:{TEST_SECRET}@redis:6379/0",
        "specialist_config_path": str(ROOT / "specialist.toml"),
    }


def test_valid_production_configuration(production_values) -> None:
    settings = Settings(_env_file=None, **production_values)
    assert settings.is_production
    assert TEST_SECRET not in repr(settings)
    assert settings.db_pool_size + settings.db_max_overflow == 5


@pytest.mark.parametrize(
    "field",
    [
        "telegram_bot_token",
        "telegram_webhook_base_url",
        "telegram_webhook_header_secret",
        "database_url",
        "redis_url",
        "specialist_config_path",
    ],
)
def test_production_requires_explicit_values(production_values, field) -> None:
    production_values.pop(field)
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, **production_values)
    assert field.upper() in str(error.value)
    assert TEST_SECRET not in str(error.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("telegram_webhook_base_url", "http://booking.example.org"),
        ("telegram_webhook_base_url", "https://user:private@booking.example.org"),
        ("telegram_webhook_base_url", "https://booking.example.org?secret=private"),
        ("telegram_webhook_header_secret", ""),
        ("telegram_webhook_header_secret", "change-me"),
        ("telegram_webhook_header_secret", "a" * 64),
        ("telegram_webhook_header_secret", "0123456789" * 4),
        ("telegram_webhook_header_secret", "change-me-to-a-long-production-secret"),
        ("telegram_webhook_header_secret", "contains spaces and an invalid / character"),
        ("telegram_bot_token", ""),
        ("telegram_bot_token", "123456:test-token"),
        ("database_url", "postgresql+asyncpg://booking:booking@postgres/booking"),
        ("database_url", "postgresql+asyncpg://booking@postgres/booking"),
        ("database_url", "sqlite:///booking.db"),
        ("database_url", "malformed connection string"),
        ("redis_url", "redis://redis:6379/0"),
        ("redis_url", "redis://:password@redis:6379/0"),
        ("redis_url", f"redis://:{TEST_SECRET}@redis:invalid/0"),
        ("specialist_config_path", ""),
        ("specialist_config_path", "missing-specialist.toml"),
    ],
)
def test_reject_unsafe_production_settings(production_values, field, value) -> None:
    production_values[field] = value
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, **production_values)
    assert TEST_SECRET not in str(error.value)
    assert "private" not in str(error.value)


def test_development_defaults_are_supported() -> None:
    assert not Settings(_env_file=None, app_env="development").is_production
    assert Settings(_env_file=None, telegram_webhook_base_url="").telegram_webhook_base_url is None


def test_internal_production_keeps_security_without_public_url(production_values) -> None:
    production_values.pop("telegram_webhook_base_url")
    settings = Settings(_env_file=None, telegram_webhook_mode="internal", **production_values)
    assert settings.is_production
    assert settings.telegram_webhook_base_url is None
    production_values["telegram_webhook_header_secret"] = "weak"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, telegram_webhook_mode="internal", **production_values)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('timezone = "Europe/Moscow"', 'timezone = "Invalid/Timezone"'),
        ("duration_minutes = 60", "duration_minutes = -1"),
        ("duration_minutes = 60", "duration_minutes = true"),
        ("requires_approval = false", 'requires_approval = "false"'),
        ("price_minor = 200000", "price_minor = -1"),
        ('monday = "10:00-19:00"', 'monday = "19:00-10:00"'),
        ('monday = "10:00-19:00"', 'noday = "10:00-19:00"'),
        ('brand_name = "Anna Tattoo"', "brand_name = 123"),
        ('key = "tattoo-session"', 'key = "consultation"'),
        ("[texts]", "[texts]\nbad = [123]"),
    ],
)
def test_invalid_specialist_config_fails_before_startup(
    production_values,
    tmp_path,
    old,
    new,
) -> None:
    original = (ROOT / "specialist.toml").read_text(encoding="utf-8")
    assert old in original
    config = tmp_path / "specialist.toml"
    config.write_text(original.replace(old, new), encoding="utf-8")
    with pytest.raises(SpecialistConfigError):
        load_specialist_template(config)
    production_values["specialist_config_path"] = str(config)
    with pytest.raises(ValidationError, match="SPECIALIST_CONFIG_PATH"):
        Settings(_env_file=None, **production_values)


def test_malformed_toml_error_does_not_include_contents(production_values, tmp_path) -> None:
    config = tmp_path / "invalid.toml"
    config.write_text(f"[broken {TEST_SECRET}", encoding="utf-8")
    production_values["specialist_config_path"] = str(config)
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, **production_values)
    assert TEST_SECRET not in str(error.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("TELEGRAM_WEBHOOK_HEADER_SECRET", None),
        ("TELEGRAM_WEBHOOK_BASE_URL", "http://booking.example.org"),
        ("SPECIALIST_CONFIG_PATH", "missing.toml"),
    ],
)
def test_server_process_fails_before_listening(production_values, tmp_path, field, value) -> None:
    environment = os.environ.copy()
    environment.update({key.upper(): str(value) for key, value in production_values.items()})
    if value is None:
        environment.pop(field, None)
    else:
        environment[field] = value
    result = subprocess.run(
        [sys.executable, "-m", "booking_bot.server"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "Invalid application configuration" in result.stderr
    assert field in result.stderr
    assert TEST_SECRET not in result.stderr

import re
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import StrictBool, StrictInt, TypeAdapter, ValidationError

from booking_bot.domain.enums import PricingMode


class SpecialistConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProfileConfig:
    slug: str
    brand_name: str
    specialist_name: str
    specialist_role: str
    bio: str
    timezone: str
    locale: str
    currency: str


@dataclass(frozen=True, slots=True)
class LocationConfig:
    name: str
    address: str


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    key: str
    name: str
    description: str
    duration_minutes: StrictInt
    buffer_before_minutes: StrictInt
    buffer_after_minutes: StrictInt
    price_minor: StrictInt | None
    requires_approval: StrictBool
    pricing_mode: PricingMode = PricingMode.FIXED


@dataclass(frozen=True, slots=True)
class SpecialistTemplate:
    profile: ProfileConfig
    location: LocationConfig
    services: tuple[ServiceConfig, ...]
    schedule: dict[str, str]
    texts: dict[str, str] = field(default_factory=dict)
    buttons: dict[str, str] = field(default_factory=dict)

    def text(self, key: str, default: str, **values: Any) -> str:
        template = self.texts.get(key, default)
        context = {
            "brand_name": self.profile.brand_name,
            "specialist_name": self.profile.specialist_name,
            "specialist_role": self.profile.specialist_role,
            "specialist_bio": self.profile.bio,
            **values,
        }
        return template.format_map(context)

    def button(self, key: str, default: str) -> str:
        template = self.buttons.get(key, default)
        return template.format_map(
            {
                "brand_name": self.profile.brand_name,
                "specialist_name": self.profile.specialist_name,
                "specialist_role": self.profile.specialist_role,
            }
        )


_template_adapter = TypeAdapter(SpecialistTemplate)


def load_specialist_template(path: str | Path) -> SpecialistTemplate:
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    if not config_path.is_file():
        raise SpecialistConfigError(f"Specialist config not found: {config_path}")
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        template = _template_adapter.validate_python(data)
        validate_specialist_template(template)
    except (OSError, ValueError, TypeError, ValidationError, ZoneInfoNotFoundError):
        raise SpecialistConfigError("Invalid specialist configuration") from None
    return template


def validate_specialist_template(template: SpecialistTemplate) -> None:
    profile = template.profile
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", profile.slug):
        raise ValueError("Invalid specialist slug")
    for value, maximum in (
        (profile.brand_name, 160),
        (profile.specialist_name, 160),
        (profile.specialist_role, 160),
        (profile.timezone, 64),
        (profile.locale, 10),
        (template.location.name, 160),
    ):
        if not value.strip() or len(value) > maximum:
            raise ValueError("Invalid profile field")
    ZoneInfo(profile.timezone)
    if not re.fullmatch(r"[A-Z]{3}", profile.currency):
        raise ValueError("Invalid currency")
    if not template.services:
        raise SpecialistConfigError("At least one service must be configured")
    if len({service.key for service in template.services}) != len(template.services):
        raise SpecialistConfigError("Service keys must be unique")
    for service in template.services:
        if (
            not service.key.strip()
            or len(service.key) > 80
            or not service.name.strip()
            or len(service.name) > 160
            or service.duration_minutes <= 0
            or service.buffer_before_minutes < 0
            or service.buffer_after_minutes < 0
            or (service.price_minor is not None and service.price_minor < 0)
        ):
            raise ValueError("Invalid service")
    weekdays = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
    for day, interval in template.schedule.items():
        if day not in weekdays:
            raise ValueError("Invalid schedule day")
        if not interval.strip():
            continue
        if not re.fullmatch(r"\d{2}:\d{2}-\d{2}:\d{2}", interval.strip()):
            raise ValueError("Invalid schedule interval")
        start, end = map(time.fromisoformat, interval.strip().split("-"))
        if start >= end:
            raise ValueError("Invalid schedule interval")


def get_specialist_template() -> SpecialistTemplate:
    from booking_bot.config import get_settings

    return load_specialist_template(get_settings().specialist_config_path)

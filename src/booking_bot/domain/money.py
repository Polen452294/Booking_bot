"""Money formatting shared by Telegram screens and notifications."""

import re
from decimal import Decimal


def format_money(amount_minor: int, currency: str) -> str:
    whole, cents = divmod(amount_minor, 100)
    amount = f"{whole:,}".replace(",", " ")
    if cents:
        amount += f",{cents:02d}"
    return f"{amount} { {'RUB': '₽', 'USD': '$', 'EUR': '€'}.get(currency, currency) }"


def parse_money(text: str, currency: str) -> int:
    value = text.strip()
    suffix = {"RUB": "₽", "USD": "$", "EUR": "€"}.get(currency, currency)
    for unit in (currency, suffix):
        if value.upper().endswith(unit):
            value = value[: -len(unit)].strip()
            break
    value = value.replace(" ", "").replace(",", ".")
    if re.fullmatch(r"\d{1,10}(?:\.\d{1,2})?", value) is None:
        raise ValueError("Invalid amount")
    amount = int(Decimal(value) * 100)
    if amount > 2_000_000_000:
        raise ValueError("Invalid amount")
    return amount

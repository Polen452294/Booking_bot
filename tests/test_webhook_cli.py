import argparse
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import WebhookInfo

from booking_bot import cli
from booking_bot.config import Settings


@pytest.fixture
def telegram(monkeypatch):
    settings = Settings(
        _env_file=None,
        telegram_bot_token="123456:test-token-private",
        telegram_webhook_header_secret="private-secret",
        telegram_webhook_base_url="https://bot.test",
    )
    bot = AsyncMock()
    bot.get_me.return_value = SimpleNamespace(username="test_bot")
    bot.get_webhook_info.return_value = WebhookInfo(
        url="https://bot.test/api/v1/webhooks/telegram",
        has_custom_certificate=False,
        pending_update_count=0,
        max_connections=40,
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "create_telegram_bot", lambda *_: bot)
    return bot


async def test_set_webhook_verifies_telegram_configuration(telegram, capsys):
    await cli.set_webhook(argparse.Namespace())
    telegram.set_webhook.assert_awaited_once()
    assert telegram.set_webhook.call_args.kwargs["drop_pending_updates"] is False
    telegram.get_webhook_info.assert_awaited_once()
    telegram.session.close.assert_awaited_once()
    output = capsys.readouterr().out
    assert "Status: OK" in output
    assert "private" not in output


async def test_internal_mode_never_registers_webhook(telegram, monkeypatch):
    settings = Settings(_env_file=None, telegram_webhook_mode="internal")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    with pytest.raises(RuntimeError, match="disabled"):
        await cli.set_webhook(argparse.Namespace())
    telegram.set_webhook.assert_not_awaited()


async def test_webhook_status_mismatch_exits_nonzero(telegram, capsys):
    telegram.get_webhook_info.return_value = telegram.get_webhook_info.return_value.model_copy(
        update={"url": "https://wrong.test"}
    )
    with pytest.raises(SystemExit) as exc:
        await cli.webhook_status(argparse.Namespace())
    assert exc.value.code == 1
    assert "Status: ERROR" in capsys.readouterr().out
    telegram.set_webhook.assert_not_awaited()
    telegram.session.close.assert_awaited_once()


async def test_webhook_status_reports_backlog_and_historical_error(telegram, capsys):
    telegram.get_webhook_info.return_value = telegram.get_webhook_info.return_value.model_copy(
        update={
            "pending_update_count": 2,
            "last_error_message": "private-secret 123456:test-token-private failed",
            "last_error_date": datetime.now(UTC),
        }
    )
    await cli.webhook_status(argparse.Namespace())
    output = capsys.readouterr().out
    assert "Pending updates: 2" in output
    assert "Last error date:" in output
    assert "Status: WARNING" in output
    assert "private" not in output

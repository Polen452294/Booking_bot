from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from booking_bot.config import Settings, get_settings
from booking_bot.db.session import get_session
from booking_bot.main import create_app
from booking_bot.services import telegram_webhook as webhook
from booking_bot.services.update_idempotency import UpdateInProgressError

PATH = "/api/v1/webhooks/telegram"
HEADERS = {"X-Telegram-Bot-Api-Secret-Token": "valid-secret"}
PAYLOAD = {
    "update_id": 1,
    "message": {"message_id": 1, "date": 1700000000, "chat": {"id": 1, "type": "private"}},
}


@pytest.fixture
def webhook_app(monkeypatch):
    settings = Settings(
        _env_file=None,
        telegram_bot_token="123456:test-token",
        telegram_webhook_header_secret="valid-secret",
    )
    app = create_app()
    session, lease, bot = AsyncMock(), AsyncMock(), AsyncMock()
    feed = AsyncMock()
    context = AsyncMock(return_value=SimpleNamespace(business_id="business", master_id="master"))
    receipt = AsyncMock(return_value=True)
    lease.acquire.return_value = True
    monkeypatch.setattr(webhook, "UpdateLease", lambda *_: lease)
    monkeypatch.setattr(webhook, "claim_receipt", receipt)
    monkeypatch.setattr(webhook, "get_specialist_context", context)
    monkeypatch.setattr(webhook, "create_telegram_bot", lambda *_: bot)

    async def dispatch(*args, commit_update, **kwargs):
        await feed(*args, **kwargs)
        await commit_update()

    monkeypatch.setattr(webhook.dispatcher, "feed_update", dispatch)

    async def db():
        yield session

    app.dependency_overrides[get_session] = db
    app.dependency_overrides[get_settings] = lambda: settings
    return SimpleNamespace(
        app=app, session=session, lease=lease, bot=bot, feed=feed, context=context, receipt=receipt
    )


async def post(case, **kwargs):
    async with AsyncClient(
        transport=ASGITransport(app=case.app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        return await client.post(PATH, **kwargs)


@pytest.mark.parametrize("secret", [None, "wrong", "секрет"])
async def test_secret_rejected_before_processing(webhook_app, secret):
    headers = {} if secret is None else {"X-Telegram-Bot-Api-Secret-Token": secret.encode()}
    response = await post(webhook_app, headers=headers, json=PAYLOAD)
    assert response.status_code == 403
    webhook_app.lease.acquire.assert_not_awaited()
    webhook_app.feed.assert_not_awaited()


async def test_valid_secret_commits_before_processed(webhook_app):
    async def finish():
        webhook_app.session.commit.assert_awaited_once()

    webhook_app.lease.finish.side_effect = finish
    response = await post(webhook_app, headers=HEADERS, json=PAYLOAD)
    assert response.status_code == 200
    webhook_app.feed.assert_awaited_once()
    webhook_app.lease.finish.assert_awaited_once()
    webhook_app.bot.session.close.assert_awaited_once()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"update_id": "1"},
        {"update_id": True},
        {"update_id": -1},
        {"update_id": []},
        {"update_id": 2**64},
        {"update_id": 1, "message": "wrong"},
        [],
    ],
)
async def test_malformed_payload(webhook_app, payload):
    assert (await post(webhook_app, headers=HEADERS, json=payload)).status_code == 422
    webhook_app.feed.assert_not_awaited()
    webhook_app.lease.acquire.assert_not_awaited()


async def test_empty_body(webhook_app):
    assert (await post(webhook_app, headers=HEADERS, content=b"")).status_code == 422


async def test_unknown_update_type_is_forward_compatible(webhook_app):
    response = await post(webhook_app, headers=HEADERS, json={"update_id": 1, "future_type": {}})
    assert response.status_code == 200
    webhook_app.feed.assert_not_awaited()


async def test_processed_duplicate_does_not_execute(webhook_app):
    webhook_app.lease.acquire.return_value = False
    assert (await post(webhook_app, headers=HEADERS, json=PAYLOAD)).status_code == 200
    webhook_app.feed.assert_not_awaited()


@pytest.mark.parametrize("failure", ["redis", "processing", "database", "commit", "handler"])
async def test_failures_are_not_acknowledged(webhook_app, failure):
    if failure == "redis":
        webhook_app.lease.acquire.side_effect = RedisConnectionError("offline")
    elif failure == "processing":
        webhook_app.lease.acquire.side_effect = UpdateInProgressError()
    elif failure == "database":
        webhook_app.receipt.side_effect = OperationalError("SELECT", {}, Exception())
    elif failure == "commit":
        webhook_app.session.commit.side_effect = OperationalError("COMMIT", {}, Exception())
    else:
        webhook_app.feed.side_effect = RuntimeError("handler failed")
    response = await post(webhook_app, headers=HEADERS, json=PAYLOAD)
    assert response.status_code >= 500
    webhook_app.lease.finish.assert_not_awaited()
    if failure in {"redis", "processing", "database"}:
        webhook_app.feed.assert_not_awaited()
    if failure in {"database", "commit", "handler"}:
        webhook_app.session.rollback.assert_awaited_once()
        webhook_app.lease.release.assert_awaited_once()

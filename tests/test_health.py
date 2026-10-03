from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from booking_bot.api.routes import health
from booking_bot.config import Settings, get_settings
from booking_bot.db.session import get_session
from booking_bot.main import create_app
from booking_bot.services.specialist_context import SpecialistNotConfiguredError
from booking_bot.specialist_config import get_specialist_template


class FakeSession:
    async def execute(self, _statement) -> None:
        return None


async def test_liveness() -> None:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.fixture
def ready_app(monkeypatch):
    app = create_app()
    session = AsyncMock()
    redis = AsyncMock()
    context = SimpleNamespace(
        business=SimpleNamespace(slug=get_specialist_template().profile.slug),
        business_id="business",
        master=SimpleNamespace(business_id="business"),
    )
    context_mock = AsyncMock(return_value=context)
    monkeypatch.setattr(health, "get_specialist_context", context_mock)

    async def override_session() -> AsyncIterator[FakeSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[health.get_readiness_redis] = lambda: redis
    return app, session, redis, context_mock


async def test_readiness_with_available_dependencies(ready_app) -> None:
    app, _, redis, _ = ready_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    redis.ping.assert_awaited_once()


@pytest.mark.parametrize("failure", ["postgres", "redis", "context", "mismatch", "config"])
async def test_readiness_dependency_failures_are_safe(ready_app, failure, tmp_path) -> None:
    app, session, redis, context_mock = ready_app
    if failure == "postgres":
        session.execute.side_effect = ConnectionError("postgresql://user:private@host/db")
    elif failure == "redis":
        redis.ping.side_effect = ConnectionError("redis://:private@host/0")
    elif failure == "context":
        context_mock.side_effect = SpecialistNotConfiguredError
    elif failure == "mismatch":
        context_mock.return_value.business.slug = "different-specialist"
    else:
        path = tmp_path / "invalid.toml"
        path.write_text("invalid TOML", encoding="utf-8")
        app.dependency_overrides[get_settings] = lambda: Settings(
            _env_file=None,
            specialist_config_path=str(path),
        )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {"detail": "Service is not ready"}
        assert (await client.get("/live")).json() == {"status": "ok"}


async def test_production_http_error_is_generic_and_http_exceptions_are_preserved(
    monkeypatch,
    caplog,
) -> None:
    from booking_bot import main

    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(
            app_name="Test",
            is_production=True,
        ),
    )
    app = main.create_app()

    @app.get("/unexpected")
    async def unexpected():
        raise RuntimeError("private-token postgresql://user:password@host/db")

    @app.get("/expected")
    async def expected():
        raise HTTPException(status_code=409, detail="Conflict")

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get("/unexpected")
        assert response.status_code == 500
        assert response.json() == {"detail": "Internal server error"}
        expected_response = await client.get("/expected")
        assert expected_response.status_code == 409
        assert expected_response.json() == {"detail": "Conflict"}
        assert expected_response.headers["x-content-type-options"] == "nosniff"
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/redoc")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404
    assert "Unhandled HTTP error" in caplog.text


async def test_single_webhook_rejects_invalid_header_secret() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        telegram_bot_token="123456:test-token",
        telegram_webhook_header_secret="expected-secret",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/webhooks/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
            json={"update_id": 1},
        )

    assert response.status_code == 403


async def test_multi_bot_webhook_path_no_longer_exists() -> None:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/webhooks/telegram/legacy-bot-secret",
            json={"update_id": 1},
        )

    assert response.status_code == 404

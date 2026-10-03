import argparse
import asyncio
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from booking_bot import cli, main
from booking_bot.config import Settings
from booking_bot.lifecycle import shutdown_event
from booking_bot.services.notification_delivery import NotificationDeliveryService


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_signals_request_shutdown_and_handlers_are_restored(sig) -> None:
    previous = signal.getsignal(sig)
    with shutdown_event() as stop:
        signal.raise_signal(sig)
        await asyncio.wait_for(stop.wait(), timeout=1)
    assert signal.getsignal(sig) == previous


async def test_idle_worker_stops_without_waiting_for_poll_interval() -> None:
    service = NotificationDeliveryService(
        Settings(
            _env_file=None,
            notification_poll_interval_seconds=60,
        )
    )
    service.run_once = AsyncMock(return_value=0)
    stop = asyncio.Event()
    task = asyncio.create_task(service.run_forever(AsyncMock(), business_id=uuid4(), stop=stop))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    service.run_once.assert_awaited_once()


async def test_worker_finishes_inflight_send_and_releases_unstarted_jobs() -> None:
    service = NotificationDeliveryService(Settings(_env_file=None))
    jobs = [uuid4(), uuid4()]
    service._claim_jobs = AsyncMock(return_value=jobs)
    service._release_jobs = AsyncMock()
    entered = asyncio.Event()
    finish = asyncio.Event()
    stop = asyncio.Event()

    async def deliver(*args, **kwargs):
        entered.set()
        await finish.wait()

    service._deliver_job = AsyncMock(side_effect=deliver)
    task = asyncio.create_task(service.run_forever(AsyncMock(), business_id=uuid4(), stop=stop))
    await entered.wait()
    stop.set()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    await asyncio.wait_for(task, timeout=1)
    service._deliver_job.assert_awaited_once()
    service._release_jobs.assert_awaited_once_with(jobs[1:])


async def test_cancelled_send_keeps_ambiguous_job_for_recovery() -> None:
    service = NotificationDeliveryService(Settings(_env_file=None))
    jobs = [uuid4(), uuid4()]
    service._claim_jobs = AsyncMock(return_value=jobs)
    service._deliver_job = AsyncMock(side_effect=asyncio.CancelledError)
    service._release_jobs = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await service.run_once(AsyncMock(), business_id=uuid4())
    service._release_jobs.assert_awaited_once_with(jobs[1:])


async def test_api_cleanup_runs_after_lifespan_failure(monkeypatch) -> None:
    close = AsyncMock(side_effect=RuntimeError("close failure"))
    dispose = AsyncMock()
    monkeypatch.setattr(main.dispatcher.fsm, "close", close)
    monkeypatch.setattr(main, "engine", SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(main, "configure_logging", lambda _: None)
    with pytest.raises(RuntimeError, match="close failure"):
        async with main.lifespan(main.create_app()):
            pass
    close.assert_awaited_once()
    dispose.assert_awaited_once()


async def test_worker_closes_bot_fsm_and_database_on_failure(monkeypatch) -> None:
    bot = AsyncMock()
    heartbeat_redis = AsyncMock()
    fsm_close, dispose = AsyncMock(), AsyncMock()
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            telegram_bot_token="123456:test-token",
        ),
    )
    # The factory is synchronous and returns an async context manager.
    session = AsyncMock()
    monkeypatch.setattr(cli, "async_session_factory", lambda: session)
    monkeypatch.setattr(
        cli,
        "get_specialist_context",
        AsyncMock(
            return_value=SimpleNamespace(business_id=uuid4()),
        ),
    )
    monkeypatch.setattr(cli, "create_telegram_bot", lambda *_: bot)
    monkeypatch.setattr(cli, "create_health_redis", lambda *_: heartbeat_redis)
    monkeypatch.setattr(
        NotificationDeliveryService,
        "run_forever",
        AsyncMock(
            side_effect=RuntimeError("worker failure"),
        ),
    )
    monkeypatch.setattr(cli.dispatcher.fsm, "close", fsm_close)
    monkeypatch.setattr(cli, "engine", SimpleNamespace(dispose=dispose))
    with pytest.raises(RuntimeError, match="worker failure"):
        await cli.async_main(argparse.Namespace(command="run-worker", once=False))
    bot.session.close.assert_awaited_once()
    heartbeat_redis.delete.assert_awaited_once()
    heartbeat_redis.aclose.assert_awaited_once()
    fsm_close.assert_awaited_once()
    dispose.assert_awaited_once()


async def test_polling_closes_bot_after_startup_failure(monkeypatch) -> None:
    bot = AsyncMock()
    bot.get_me.side_effect = RuntimeError("network failure")
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            app_env="development",
            telegram_bot_token="123456:test-token",
        ),
    )
    monkeypatch.setattr(cli, "async_session_factory", lambda: AsyncMock())
    monkeypatch.setattr(
        cli,
        "get_specialist_context",
        AsyncMock(
            return_value=SimpleNamespace(master=SimpleNamespace(user_id=None)),
        ),
    )
    monkeypatch.setattr(cli, "create_telegram_bot", lambda *_: bot)
    fsm_close, dispose = AsyncMock(), AsyncMock()
    monkeypatch.setattr(cli.dispatcher.fsm, "close", fsm_close)
    monkeypatch.setattr(cli, "engine", SimpleNamespace(dispose=dispose))
    with pytest.raises(RuntimeError, match="network failure"):
        await cli.async_main(argparse.Namespace(command="run-polling"))
    bot.session.close.assert_awaited_once()
    fsm_close.assert_awaited_once()
    dispose.assert_awaited_once()

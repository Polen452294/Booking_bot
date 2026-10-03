import asyncio
import socket
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import text

from booking_bot.config import Settings
from booking_bot.db.session import async_session_factory

HEARTBEAT_TTL = 120
HEARTBEAT_INTERVAL = 30


def create_health_redis(settings: Settings) -> Redis:
    return Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)


class WorkerHeartbeat:
    def __init__(self, redis: Redis, settings: Settings, worker_id: str | None = None) -> None:
        self.redis = redis
        # Hostname is unique per Docker container: a healthy replica cannot mask a stuck one.
        self.key = (
            f"{settings.redis_namespace}:worker:heartbeat:{worker_id or socket.gethostname()}"
        )

    async def beat(self) -> None:
        await self.redis.set(self.key, datetime.now(UTC).isoformat(), ex=HEARTBEAT_TTL)

    async def clear(self) -> None:
        await self.redis.delete(self.key)


async def check_worker_health(settings: Settings, worker_id: str | None = None) -> bool:
    database_ok = redis_ok = worker_ok = False
    last_heartbeat = None
    try:
        async with asyncio.timeout(4), async_session_factory() as session:
            await session.execute(text("SELECT 1"))
        database_ok = True
    except Exception:
        pass
    redis = create_health_redis(settings)
    try:
        async with asyncio.timeout(4):
            await redis.ping()
            redis_ok = True
            value = await redis.get(WorkerHeartbeat(redis, settings, worker_id).key)
            if value:
                last_heartbeat = datetime.fromisoformat(value.decode())
                age = (datetime.now(UTC) - last_heartbeat).total_seconds()
                worker_ok = 0 <= age < HEARTBEAT_TTL
    except Exception:
        pass
    finally:
        await redis.aclose()
    print(f"Database: {'OK' if database_ok else 'UNHEALTHY'}")
    print(f"Redis: {'OK' if redis_ok else 'UNHEALTHY'}")
    print(f"Worker: {'OK' if worker_ok else 'UNHEALTHY'}")
    print(f"Last heartbeat: {last_heartbeat.isoformat() if last_heartbeat else 'missing/expired'}")
    return database_ok and redis_ok and worker_ok

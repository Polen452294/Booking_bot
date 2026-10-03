from datetime import UTC, datetime, timedelta
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy import delete, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from booking_bot.db.models import TelegramUpdateReceipt

# Telegram retains incoming updates for at most 24h; allow a generous replay margin.
PROCESSED_TTL = 7 * 24 * 60 * 60
PROCESSING_TTL = 120
PROCESSING_TIMEOUT = 60

_ACQUIRE = """
local value = redis.call('GET', KEYS[1])
if value == 'processed' then return 2 end
if value then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""
_FINISH = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], 'processed', 'EX', ARGV[2])
return 1
"""
_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class UpdateInProgressError(RuntimeError):
    pass


class UpdateLease:
    def __init__(self, redis: Redis, namespace: str, update_id: int) -> None:
        self.redis = redis
        self.key = f"{namespace}:telegram:update:{update_id}"
        self.token = f"processing:{uuid4().hex}"

    async def acquire(self) -> bool:
        result = await self.redis.eval(_ACQUIRE, 1, self.key, self.token, PROCESSING_TTL)
        if result == 0:
            # A 200 here would acknowledge an update whose first attempt can still fail.
            raise UpdateInProgressError
        return result == 1

    async def finish(self) -> None:
        if not await self.redis.eval(_FINISH, 1, self.key, self.token, PROCESSED_TTL):
            raise UpdateInProgressError

    async def release(self) -> None:
        await self.redis.eval(_RELEASE, 1, self.key, self.token)


async def claim_receipt(session: AsyncSession, namespace: str, update_id: int) -> bool:
    """Commit this receipt together with business writes, never before them.

    The unique key also serializes transactions if a Redis lease expires or is lost.
    A rollback removes the receipt; a lost commit acknowledgement remains replay-safe.
    """
    now = datetime.now(UTC)
    if update_id % 100 == 0:
        expired = (
            select(TelegramUpdateReceipt.namespace, TelegramUpdateReceipt.update_id)
            .where(TelegramUpdateReceipt.expires_at < now)
            .limit(1000)
            .with_for_update(skip_locked=True)
        )
        await session.execute(
            delete(TelegramUpdateReceipt).where(
                tuple_(TelegramUpdateReceipt.namespace, TelegramUpdateReceipt.update_id).in_(
                    expired
                )
            )
        )
    receipt = await session.scalar(
        insert(TelegramUpdateReceipt)
        .values(
            namespace=namespace,
            update_id=update_id,
            expires_at=now + timedelta(seconds=PROCESSED_TTL),
        )
        .on_conflict_do_update(
            index_elements=["namespace", "update_id"],
            set_={"expires_at": now + timedelta(seconds=PROCESSED_TTL)},
            where=TelegramUpdateReceipt.expires_at < now,
        )
        .returning(TelegramUpdateReceipt.update_id)
    )
    return receipt is not None

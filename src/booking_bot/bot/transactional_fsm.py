from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage


class BufferedFSMContext(FSMContext):
    """Keep webhook FSM writes local until the business transaction commits."""

    def __init__(self, original: FSMContext) -> None:
        super().__init__(MemoryStorage(), original.key)
        self.original = original

    async def load(self, raw_state: str | None) -> None:
        await self.set_state(raw_state)
        await self.set_data(await self.original.get_data())

    async def apply(self) -> None:
        storage = self.original.storage
        if not isinstance(storage, RedisStorage):
            raise TypeError("Webhook FSM requires RedisStorage")
        state, data = await self.get_state(), await self.get_data()
        # State + data become visible together while the per-chat Redis lock is held.
        async with storage.redis.pipeline(transaction=True) as pipeline:
            state_key = storage.key_builder.build(self.key, "state")
            data_key = storage.key_builder.build(self.key, "data")
            if state is None:
                pipeline.delete(state_key)
            else:
                pipeline.set(state_key, state, ex=storage.state_ttl)
            if not data:
                pipeline.delete(data_key)
            else:
                pipeline.set(data_key, storage.json_dumps(data), ex=storage.data_ttl)
            await pipeline.execute()

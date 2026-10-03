import asyncio
import signal
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def shutdown_event() -> Iterator[asyncio.Event]:
    """Works for CLI entry points on Windows as well as Unix; restore prior handlers."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        loop.call_soon_threadsafe(stop.set)

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for sig in previous:
            signal.signal(sig, request_stop)
        yield stop
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

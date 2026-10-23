"""Readable console events and progress messages for blocking benchmark operations."""

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from threading import Event, Lock, Thread
from time import monotonic
from zoneinfo import ZoneInfo

PROGRESS_INTERVAL_SECONDS = 30
_OUTPUT_LOCK = Lock()


def log(message: str, *, label: str = "INFO", blank: bool = False) -> None:
    prefix = f"{datetime.now(ZoneInfo('Asia/Kolkata')):%H:%M:%S}  [{label}] "
    text = ("\n" if blank else "") + "\n".join(prefix + line for line in message.splitlines()) + "\n"
    with _OUTPUT_LOCK:
        sys.stdout.write(text)
        sys.stdout.flush()


@contextmanager
def activity(message: str, *, label: str) -> Iterator[None]:
    """Announce an operation and keep reporting while it blocks; always stop the heartbeat."""
    log(message, label=label)
    started = monotonic()
    stopped = Event()

    def heartbeat() -> None:
        while not stopped.wait(PROGRESS_INTERVAL_SECONDS):
            log(f"{message} ({monotonic() - started:.0f}s elapsed)", label="WAIT")

    worker = Thread(target=heartbeat, name="benchmark-progress", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join()

"""Small polling helpers for GUI state transitions."""
from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")


def wait_until(
    probe: Callable[[], T],
    *,
    accept: Callable[[T], bool] = bool,
    timeout_sec: float = 5.0,
    interval_sec: float = 0.15,
) -> T:
    """
    Poll *probe* until *accept(result)* is true, then return that result.

    Raises TimeoutError with the last probe result attached to the message.
    """
    deadline = time.monotonic() + timeout_sec
    last: T | None = None
    while True:
        last = probe()
        if accept(last):
            return last
        if time.monotonic() >= deadline:
            raise TimeoutError(f"condition not met within {timeout_sec:.1f}s; last={last!r}")
        time.sleep(interval_sec)


def sleep_until_hash_stable(
    capture_hash: Callable[[], str],
    *,
    samples: int = 2,
    interval_sec: float = 0.20,
    timeout_sec: float = 6.0,
) -> str:
    """Wait until the same screenshot hash appears *samples* times in a row."""
    if samples <= 1:
        return capture_hash()

    prev: str | None = None
    streak = 0
    deadline = time.monotonic() + timeout_sec
    last = ""
    while True:
        last = capture_hash()
        if last == prev:
            streak += 1
        else:
            prev = last
            streak = 1
        if streak >= samples:
            return last
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"screenshot hash did not stabilise within {timeout_sec:.1f}s; last={last[:12]}"
            )
        time.sleep(interval_sec)

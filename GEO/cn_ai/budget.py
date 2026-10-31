"""One process deadline shared by browser work and its optional stages."""
from __future__ import annotations

import contextvars
import asyncio
import math
import os
import signal
import threading
import time
from contextlib import contextmanager

_stage_end = contextvars.ContextVar("geo_stage_end", default=None)


def seconds(name, default, maximum=60):
    try:
        return max(.01, min(maximum, float(os.getenv(name, str(default)))))
    except ValueError:
        return default


class StageDeadline(BaseException):
    # Browser/share helpers deliberately catch Exception for recoverable UI
    # errors. A deadline must unwind those handlers rather than be swallowed.
    def __init__(self, code):
        super().__init__(code)
        self.error_code = code


def remaining(limit=None, reserve=0):
    end = _stage_end.get()
    try:
        configured = float(os.getenv("GEO_DIAGNOSTIC_DEADLINE_MS", "0")) / 1000
    except (ValueError, TypeError):
        configured = 0
    if configured > 0:
        end = min(end, configured - reserve) if end is not None else configured - reserve
    seconds = max(0, end - time.time()) if end is not None else float("inf")
    return min(seconds, max(0, limit)) if limit is not None else seconds


def timeout_ms(limit=8000, reserve=1):
    return max(0, int(remaining(limit / 1000, reserve) * 1000))


def wait_window(fallback, reserve=0):
    """Use the actual attempt window, retaining a finite standalone default."""
    available = remaining(reserve=reserve)
    return available if math.isfinite(available) else max(0, fallback)


def sleep(seconds, reserve=0):
    duration = remaining(seconds, reserve)
    if duration > 0:
        time.sleep(duration)


@contextmanager
def stage(seconds, *, reserve=1, code="answer_extract_timeout"):
    duration = remaining(seconds, reserve)
    if duration <= 0:
        raise StageDeadline(code)
    end = time.time() + duration
    token = _stage_end.set(end)
    main = threading.current_thread() is threading.main_thread() and hasattr(signal, "setitimer")
    old_handler = old_timer = None
    expired_stage = False
    started = time.monotonic()
    if main:
        old_handler = signal.getsignal(signal.SIGALRM)
        old_timer = signal.getitimer(signal.ITIMER_REAL)

        def expired(_signum, _frame):
            nonlocal expired_stage
            expired_stage = True
            # Raising through Playwright's dispatcher greenlet corrupts its
            # sync bridge and can hang cleanup. Cancel only the pending sync
            # API task, so the bridge unwinds through its normal done callback.
            try:
                loop = asyncio.get_running_loop()
                pending = [task for task in asyncio.all_tasks(loop) if hasattr(task, "__pw_stack__") and not task.done()]
            except RuntimeError:
                pending = []
            if pending:
                for task in pending:
                    loop.call_soon(task.cancel)
                return
            raise StageDeadline(code)

        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, max(.001, duration), .05)
    try:
        try:
            yield
        except asyncio.CancelledError:
            if expired_stage:
                raise StageDeadline(code) from None
            raise
        if time.time() > end:
            raise StageDeadline(code)
    finally:
        if main:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)
            if old_timer[0] > 0:
                signal.setitimer(signal.ITIMER_REAL, max(.001, old_timer[0] - (time.monotonic() - started)), old_timer[1])
        _stage_end.reset(token)


def call(fn, seconds, *, reserve=1, code="answer_extract_timeout"):
    with stage(seconds, reserve=reserve, code=code):
        return fn()

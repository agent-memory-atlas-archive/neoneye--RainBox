"""Stopping a single-call agent turn from the outside.

The operator presses Stop on /chat; the API flags the turn's journal row
(`journal.stop_requested_at`); the worker that owns the row notices and
abandons its model call. Two pieces live here:

- `StopRequested` — raised in the worker's main thread when the request is
  observed. The agent catches it, settles whatever it has, and re-raises as
  `TurnStopped`, which `Agent.run` journals as state `stopped`.
- `StopWatch` — the observer. A daemon thread evaluates a caller-supplied
  `is_requested()` every `poll_seconds`; when it returns true the watch sets
  an event and, if the main thread has declared itself interruptible,
  delivers SIGUSR1 to it.

The signal is what makes a *blocked* read stop. A handler that raises makes
the interrupted system call propagate the exception instead of retrying
(PEP 475) — the same mechanism the supervisor's SIGTERM relies on to unwind
a worker mid-read. So a stop works while a cold model has produced nothing
yet, not only between chunks. The handler raises only inside
`interruptible()` windows; anywhere else it does nothing and the agent picks
the request up at its next `raise_if_requested()` — its safe point between
chunks — never inside a database commit.

Design: docs/superpowers/specs/2026-09-15-chat-stop-button-design.md
"""
from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from types import FrameType
from typing import Any

logger = logging.getLogger(__name__)

STOP_SIGNAL = signal.SIGUSR1


class StopRequested(Exception):
    """The operator asked for this turn to stop; raised in the main thread."""


class TurnStopped(Exception):
    """A handled stop: the agent settled its rows and posted its notice.
    Carries the partial reply for the journal result."""

    def __init__(self, reply: str = "") -> None:
        super().__init__("stopped by operator")
        self.reply = reply


class StopWatch:
    """Watches for a stop request while an agent runs one model call.

    Usage::

        with StopWatch(probe) as watch:
            with watch.interruptible():
                stream = llm.stream_chat(messages)
            for ...:
                watch.raise_if_requested()
                with watch.interruptible():
                    chunk = next(chunks, END)

    Must be entered from the main thread (Python only runs signal handlers
    there). A `None` probe makes the watch inert — every method is then a
    no-op, so callers with no journal (tests driving the stream directly)
    need no branches.
    """

    POLL_SECONDS: float = 0.5

    def __init__(
        self, is_requested: Callable[[], bool] | None, *,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._probe = is_requested
        self._poll = poll_seconds
        self._lock = threading.Lock()
        self._event = threading.Event()   # a request has been observed
        self._halt = threading.Event()    # tells the watcher thread to exit
        self._interruptible = False
        self._thread: threading.Thread | None = None
        self._previous: Any = None
        self._main = threading.main_thread()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def raise_if_requested(self) -> None:
        """The between-chunks safe point."""
        if self._event.is_set():
            raise StopRequested()

    def __enter__(self) -> "StopWatch":
        if self._probe is None:
            return self
        if threading.current_thread() is not self._main:
            raise RuntimeError("StopWatch must be entered from the main thread")
        self._previous = signal.signal(STOP_SIGNAL, self._on_signal)
        self._thread = threading.Thread(
            target=self._watch, name="stop-watch", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> bool:
        if self._probe is None:
            return False
        # Order matters: no more raising, then no more signalling (the thread
        # is gone), and only then the old handler — a late SIGUSR1 under the
        # default disposition would terminate the process.
        with self._lock:
            self._interruptible = False
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        signal.signal(STOP_SIGNAL, self._previous)
        return False

    def _on_signal(self, _signum: int, _frame: FrameType | None) -> None:
        # Runs in the main thread, so the flag is read consistently with the
        # window that set it: outside a window the request simply waits for
        # the next raise_if_requested().
        if self._interruptible:
            raise StopRequested()

    def _watch(self) -> None:
        assert self._probe is not None
        while not self._halt.wait(self._poll):
            try:
                hit = bool(self._probe())
            except Exception:
                logger.warning("stop watch: probe failed; still watching", exc_info=True)
                continue
            if not hit:
                continue
            with self._lock:
                self._event.set()
                if self._interruptible and self._main.ident is not None:
                    signal.pthread_kill(self._main.ident, STOP_SIGNAL)
            return

    @contextmanager
    def interruptible(self) -> Generator[None, None, None]:
        """Declare a window in which the main thread may be interrupted by the
        signal — wrap only calls that block on the network. Entering with a
        request already observed raises at once; the check and the flag flip
        happen under the watcher's lock, so a request cannot land between
        them unnoticed."""
        if self._probe is None:
            yield
            return
        with self._lock:
            if self._event.is_set():
                raise StopRequested()
            self._interruptible = True
        try:
            yield
        finally:
            with self._lock:
                self._interruptible = False

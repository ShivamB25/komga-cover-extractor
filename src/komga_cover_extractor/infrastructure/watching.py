"""Watchdog intake and signal ownership without configured import-time effects."""
from __future__ import annotations

import os
import signal
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from queue import Full
from threading import Event, Thread, current_thread, main_thread
from types import FrameType
from typing import TYPE_CHECKING

from ..domain.decisions import Decision

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver

WatchEvent = tuple[str, Path, Path | None, bool]


class WatchSignalError(RuntimeError):
    pass


def start_observer(
    root: Path, enqueue: Callable[[WatchEvent], None], *, capacity: int = 256,
    polling: bool = False, interval: float = 1.0,
) -> BaseObserver:
    """Start a real observer with nonblocking, bounded upstream event intake."""
    from watchdog.events import (
        FileSystemEvent,
        FileSystemEventHandler,
        FileSystemMovedEvent,
    )
    from watchdog.observers.api import BaseObserver, EventEmitter, EventQueue

    emitter_type: type[EventEmitter]
    if polling:
        from watchdog.observers.polling import PollingEmitter
        emitter_type = PollingEmitter
    elif sys.platform == "linux":
        from watchdog.observers.inotify import InotifyEmitter
        emitter_type = InotifyEmitter
    else:
        raise NotImplementedError("native_watch_requires_linux_or_explicit_polling")

    class Intake(FileSystemEventHandler):
        def dispatch(self, event: FileSystemEvent) -> None:
            kind = event.event_type
            if kind not in {"created", "modified", "moved", "deleted", "overflow"}:
                return
            source = Path(os.fsdecode(event.src_path))
            destination = Path(os.fsdecode(event.dest_path)) if isinstance(event, FileSystemMovedEvent) else None
            enqueue((kind, source, destination, event.is_directory))

    class BoundedEvents(EventQueue):
        def put(self, item: object, block: bool = True, timeout: float | None = None) -> None:
            try:
                super().put(item, block=False)
            except Full:
                enqueue(("overflow", root, None, True))

    events = BoundedEvents(maxsize=capacity)

    class ManagedObserver(BaseObserver):
        @property
        def event_queue(self) -> EventQueue:
            return events

        def on_thread_stop(self) -> None:
            # Watchdog's default hook joins emitters without a deadline.
            for emitter in tuple(self.emitters):
                emitter.stop()

    observer = ManagedObserver(emitter_type, timeout=interval)
    try:
        observer.schedule(Intake(), str(root), recursive=True)
        observer.start()
        return observer
    except BaseException:
        close_observers((observer,), timeout=30)
        raise


def close_observers(observers: Sequence[BaseObserver], *, timeout: float = 30) -> tuple[Decision, ...]:
    """Stop every producer before joining with one shared shutdown deadline."""
    deadline = time.monotonic() + timeout
    decisions: list[Decision] = []
    threads: list[Thread] = []
    for observer in observers:
        threads.extend(tuple(observer.emitters))
        threads.append(observer)
        try:
            observer.stop()
        except (OSError, RuntimeError):
            decisions.append(Decision("failed", "watch_observer_stop_failed"))
    for thread in threads:
        if thread.ident is None:
            continue
        try:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        except RuntimeError:
            decisions.append(Decision("failed", "watch_observer_join_failed"))
            continue
        if thread.is_alive():
            decisions.append(Decision("failed", "watch_observer_shutdown_deadline"))
    return tuple(decisions)


@contextmanager
def watch_signals(cancel: Event, stop: Event, *, enabled: bool = True) -> Iterator[None]:
    """Temporarily own SIGINT/SIGTERM and restore the caller's handlers."""
    previous: dict[signal.Signals, Callable[[int, FrameType | None], object] | int | None] = {}

    def shutdown(number: int, frame: FrameType | None) -> None:
        stop.set()
        cancel.set()

    try:
        if enabled:
            if current_thread() is not main_thread():
                raise WatchSignalError("watch_signal_handlers_require_main_thread")
            for number in (signal.SIGINT, signal.SIGTERM):
                previous[number] = signal.getsignal(number)
                signal.signal(number, shutdown)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


@contextmanager
def forward_cancellation(stop: Event, cancel: Event) -> Iterator[None]:
    """Forward an independently supplied stop event during synchronous pipeline work."""
    finished = Event()
    thread: Thread | None = None
    if stop is not cancel:
        def forward() -> None:
            while not finished.wait(0.05):
                if stop.is_set():
                    cancel.set()
                    return
        thread = Thread(target=forward, name="kce-watch-cancellation")
        thread.start()
    try:
        yield
    finally:
        finished.set()
        if thread is not None:
            thread.join()

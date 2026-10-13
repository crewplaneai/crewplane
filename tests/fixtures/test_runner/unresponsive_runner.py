"""Ignore shutdown requests so the outer test must terminate this runner."""

import signal
from collections.abc import Iterator
from contextlib import contextmanager

import run_tests
import test_process


@contextmanager
def ignore_shutdown() -> Iterator[list[int]]:
    previous = {
        number: signal.signal(number, signal.SIG_IGN)
        for number in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield []
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


test_process.shutdown_signals = ignore_shutdown
raise SystemExit(run_tests.main())

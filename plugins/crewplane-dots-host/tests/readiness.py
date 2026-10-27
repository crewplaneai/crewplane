"""Bounded readiness receipt reads without buffering later diagnostics."""

import os
import re
import selectors
from time import monotonic
from typing import IO


def read_job_receipt(stream: IO[str], timeout: float = 5.0) -> str:
    deadline = monotonic() + timeout
    payload = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(stream.fileno(), selectors.EVENT_READ)
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0 or not selector.select(remaining):
                category = "incomplete" if payload else "timeout"
                raise AssertionError(f"Dots readiness {category}: deadline expired")
            chunk = os.read(stream.fileno(), 1)
            if not chunk:
                category = "incomplete" if payload else "EOF"
                raise AssertionError(f"Dots readiness {category}: child closed stderr")
            payload.extend(chunk)
            if len(payload) > 128:
                raise AssertionError("Dots readiness overlong receipt")
            if chunk == b"\n":
                break
    match = re.fullmatch(rb"dots-host job ([a-zA-Z0-9_-]+)\r?\n", payload)
    if match is None:
        raise AssertionError("Dots readiness malformed receipt")
    return match[1].decode("ascii")

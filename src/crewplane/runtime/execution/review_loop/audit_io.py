"""Retain audit state ownership until blocking I/O finishes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import copy_context


async def complete_audit_io[Result](operation: Callable[[], Result]) -> Result:
    """Await audit I/O in the default executor before releasing its inputs.

    Shield the executor future itself so runner shutdown cannot cancel an
    intermediary task while its worker still runs. Preserve context variables
    and drain the worker through repeated cancellation. Operation errors take
    precedence over cancellation, matching synchronous I/O error ordering.
    """
    worker = asyncio.get_running_loop().run_in_executor(
        None, copy_context().run, operation
    )
    cancelled: asyncio.CancelledError | None = None
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as exc:
            cancelled = exc
    result = worker.result()
    if cancelled is not None:
        raise cancelled
    return result

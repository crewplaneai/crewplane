"""Settle synchronous run lifecycle work before surrendering run ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import copy_context


async def settle_run_work[Result](
    operation: Callable[[], Result],
) -> tuple[Result, asyncio.CancelledError | None]:
    """Return settled work and the first cancellation; worker errors take precedence.

    An executor future is not an asyncio task, so runner shutdown cannot cancel
    its completion receipt while the thread still owns run artifacts or locks.
    Waiting through ``asyncio.wait`` also prevents owner cancellation from
    cancelling that receipt. Preserve the caller's context in the worker.
    """
    future = asyncio.get_running_loop().run_in_executor(
        None, copy_context().run, operation
    )
    cancellation: asyncio.CancelledError | None = None
    while not future.done():
        try:
            await asyncio.wait((future,))
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    return future.result(), cancellation


async def run_blocking_work[Result](operation: Callable[[], Result]) -> Result:
    """Finish owned work before propagating cancellation to the run lifecycle."""
    result, cancellation = await settle_run_work(operation)
    if cancellation is not None:
        raise cancellation
    return result

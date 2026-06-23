from __future__ import annotations

import asyncio

from crewplane.architecture.contracts import InvocationContext


async def reset_before_retry(invocation_context: InvocationContext | None) -> None:
    if invocation_context is None or invocation_context.retry_reset is None:
        return
    reset_task = asyncio.create_task(asyncio.to_thread(invocation_context.retry_reset))
    try:
        await asyncio.shield(reset_task)
    except asyncio.CancelledError as cancel:
        await finish_cancelled_retry_reset(invocation_context, reset_task, cancel)
        raise


async def finish_cancelled_retry_reset(
    invocation_context: InvocationContext,
    reset_task: asyncio.Task[None],
    cancel: asyncio.CancelledError,
) -> None:
    if invocation_context.retry_reset_canceller is not None:
        try:
            await asyncio.to_thread(invocation_context.retry_reset_canceller)
        except Exception as exc:
            cancel.add_note(f"Workspace retry reset cancellation failed: {exc}")
    try:
        await asyncio.shield(reset_task)
    except Exception as exc:
        cancel.add_note(f"Workspace retry reset after cancellation failed: {exc}")

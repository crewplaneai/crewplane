"""Collect concurrent invocations without losing cancellation cleanup evidence."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from crewplane.runtime.agent.process.drain import unconfirmed_process_cleanup


async def gather_invocations[T](
    calls: Sequence[Callable[[], Awaitable[T]]],
) -> list[T | BaseException]:
    async def collect(call: Callable[[], Awaitable[T]]) -> T | BaseException:
        try:
            return await call()
        except BaseException as exc:
            return exc

    tasks = [asyncio.create_task(collect(call)) for call in calls]
    try:
        return list(await asyncio.gather(*tasks))
    except asyncio.CancelledError as cancellation:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                cleanup_error = unconfirmed_process_cleanup(result)
                if cleanup_error is not None:
                    cancellation.__cause__ = cleanup_error
        raise

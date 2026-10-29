"""Serial log writes whose thread workers remain owned through cancellation."""

from __future__ import annotations

import asyncio
from typing import BinaryIO

from .session import wait_for_owned_task

LOG_QUEUE_MAX_ITEMS = 64


async def drain_log_queue(
    log_handle: BinaryIO,
    log_queue: asyncio.Queue[bytes | None],
    writer_status: asyncio.Future[BaseException | None],
) -> None:
    outcome: BaseException | None = None
    try:
        while True:
            payload = await log_queue.get()
            if payload is None:
                return
            worker = asyncio.create_task(
                asyncio.to_thread(write_log_bytes, log_handle, payload)
            )
            cancellation = await wait_for_owned_task(worker)
            try:
                worker.result()
            except BaseException as exc:
                if cancellation is not None:
                    exc.add_note("Log writing failed during cancellation.")
                raise
            if cancellation is not None:
                raise cancellation
    except BaseException as exc:
        outcome = exc
        if isinstance(exc, asyncio.CancelledError):
            raise
    finally:
        writer_status.set_result(outcome)


async def watch_log_writer_status(
    writer_status: asyncio.Future[BaseException | None],
) -> None:
    writer_error = await asyncio.shield(writer_status)
    if writer_error is not None:
        raise writer_error


async def signal_log_queue_complete(
    log_queue: asyncio.Queue[bytes | None],
    writer_status: asyncio.Future[BaseException | None],
) -> None:
    current = asyncio.current_task()
    if writer_status.done() or (current is not None and current.cancelling()):
        return
    sentinel = asyncio.create_task(log_queue.put(None))
    try:
        await asyncio.wait(
            (sentinel, writer_status), return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        if not sentinel.done():
            sentinel.cancel()
        await wait_for_owned_task(sentinel)
        if not sentinel.cancelled():
            sentinel.result()


def write_log_bytes(log_handle: BinaryIO, payload: bytes) -> None:
    log_handle.write(payload)
    log_handle.flush()

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from functools import partial
from pathlib import Path
from threading import Thread

from crewplane.architecture.contracts import InvocationContext

RETRY_RESET_DEADLINE_SECONDS = 30.0


async def reset_before_retry(invocation_context: InvocationContext | None) -> None:
    if invocation_context is None or invocation_context.retry_reset is None:
        return
    state_path = _retry_reset_state_path(invocation_context)
    if state_path is not None:
        _record_unresolved_retry_reset(state_path)
    reset_task = _daemon_retry_worker_task(
        partial(
            _run_retry_reset_worker,
            invocation_context.retry_reset,
            state_path,
        )
    )
    try:
        await asyncio.wait_for(
            asyncio.shield(reset_task),
            timeout=RETRY_RESET_DEADLINE_SECONDS,
        )
    except TimeoutError as exc:
        await _cancel_retry_reset(invocation_context, reset_task)
        raise RuntimeError(
            "Workspace retry reset exceeded its internal deadline."
        ) from exc
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
            await _await_retry_reset_canceller(invocation_context.retry_reset_canceller)
        except TimeoutError:
            cancel.add_note(
                "Workspace retry reset canceller did not stop within its deadline."
            )
        except Exception as exc:
            cancel.add_note(f"Workspace retry reset cancellation failed: {exc}")
    try:
        await asyncio.wait_for(
            asyncio.shield(reset_task),
            timeout=RETRY_RESET_DEADLINE_SECONDS,
        )
    except TimeoutError:
        reset_task.add_done_callback(_consume_retry_worker_result)
        cancel.add_note("Workspace retry reset did not stop within its deadline.")
    except Exception as exc:
        cancel.add_note(f"Workspace retry reset after cancellation failed: {exc}")


async def _cancel_retry_reset(
    invocation_context: InvocationContext,
    reset_task: asyncio.Task[None],
) -> None:
    if invocation_context.retry_reset_canceller is not None:
        with suppress(Exception):
            await _await_retry_reset_canceller(invocation_context.retry_reset_canceller)
    try:
        await asyncio.wait_for(
            asyncio.shield(reset_task),
            timeout=RETRY_RESET_DEADLINE_SECONDS,
        )
    except TimeoutError:
        reset_task.add_done_callback(_consume_retry_worker_result)
    except Exception:
        return


async def _await_retry_reset_canceller(canceller: Callable[[], None]) -> None:
    task = _daemon_retry_worker_task(canceller)
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=RETRY_RESET_DEADLINE_SECONDS,
        )
    except TimeoutError:
        task.add_done_callback(_consume_retry_worker_result)
        raise


def _daemon_retry_worker_task(worker: Callable[[], None]) -> asyncio.Task[None]:
    return asyncio.create_task(_await_daemon_retry_worker(worker))


async def _await_daemon_retry_worker(worker: Callable[[], None]) -> None:
    loop = asyncio.get_running_loop()
    completion: asyncio.Future[None] = loop.create_future()

    def run() -> None:
        error: BaseException | None = None
        try:
            worker()
        except BaseException as exc:
            error = exc
        try:
            loop.call_soon_threadsafe(_finish_retry_worker, completion, error)
        except RuntimeError:
            return

    Thread(target=run, name="crewplane-retry-reset", daemon=True).start()
    await completion


def _finish_retry_worker(
    completion: asyncio.Future[None],
    error: BaseException | None,
) -> None:
    if completion.done():
        return
    if error is not None:
        completion.set_exception(error)
        return
    completion.set_result(None)


def _consume_retry_worker_result(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    with suppress(BaseException):
        task.exception()


def _retry_reset_state_path(invocation_context: InvocationContext) -> Path | None:
    workspace = invocation_context.workspace
    return None if workspace is None else workspace.workspace_state_path


def _record_unresolved_retry_reset(state_path: Path) -> None:
    from crewplane.runtime.workspace.mutator_fence import (
        fence_workspace_mutator,
        release_workspace_mutator,
    )
    from crewplane.runtime.workspace.state import mutate_workspace_state

    fence_workspace_mutator(state_path)
    try:
        mutate_workspace_state(
            state_path,
            lambda payload: payload.__setitem__(
                "workspace_mutator",
                {"status": "unresolved", "operation": "retry_reset"},
            ),
        )
    except BaseException:
        release_workspace_mutator(state_path)
        raise


def _run_retry_reset_worker(
    retry_reset: Callable[[], None],
    state_path: Path | None,
) -> None:
    try:
        retry_reset()
    finally:
        if state_path is not None:
            _confirm_retry_reset_worker_finished(state_path)


def _confirm_retry_reset_worker_finished(state_path: Path) -> None:
    from crewplane.runtime.workspace.mutator_fence import release_workspace_mutator
    from crewplane.runtime.workspace.state import mutate_workspace_state

    try:
        mutate_workspace_state(
            state_path,
            lambda payload: payload.__setitem__(
                "workspace_mutator",
                {
                    "status": "confirmed",
                    "operation": "retry_reset",
                    "outcome": "finished",
                },
            ),
        )
    except Exception:
        pass
    finally:
        release_workspace_mutator(state_path)

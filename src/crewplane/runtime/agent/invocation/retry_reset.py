from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from threading import Thread

from crewplane.architecture.contracts import InvocationContext
from crewplane.runtime.workspace import mutator_fence as workspace_mutator_fence
from crewplane.runtime.workspace import state as workspace_state

RETRY_RESET_DEADLINE_SECONDS = 30.0


@dataclass(frozen=True)
class _RunningRetryReset:
    task: asyncio.Task[None]
    canceller: Callable[[], None] | None


async def reset_before_retry(invocation_context: InvocationContext | None) -> None:
    running_reset = _start_retry_reset(invocation_context)
    if running_reset is None:
        return
    await _await_retry_reset_completion(running_reset)


def _start_retry_reset(
    invocation_context: InvocationContext | None,
) -> _RunningRetryReset | None:
    if invocation_context is None or invocation_context.retry_reset is None:
        return None
    state_path = _retry_reset_state_path(invocation_context)
    if state_path is not None:
        _record_unresolved_retry_reset(state_path)
    worker = partial(
        _run_retry_reset_worker,
        invocation_context.retry_reset,
        state_path,
    )
    return _RunningRetryReset(
        task=_daemon_retry_worker_task(worker),
        canceller=invocation_context.retry_reset_canceller,
    )


async def _await_retry_reset_completion(running_reset: _RunningRetryReset) -> None:
    try:
        await _wait_for_retry_worker(running_reset.task)
    except TimeoutError as exc:
        await _cancel_timed_out_retry_reset(running_reset)
        raise RuntimeError(
            "Workspace retry reset exceeded its internal deadline."
        ) from exc
    except asyncio.CancelledError as cancel:
        await _finish_cancelled_retry_reset(running_reset, cancel)
        raise


async def _finish_cancelled_retry_reset(
    running_reset: _RunningRetryReset,
    cancel: asyncio.CancelledError,
) -> None:
    await _request_retry_reset_cancellation(running_reset.canceller, cancel)
    await _await_cancelled_retry_worker(running_reset.task, cancel)


async def _request_retry_reset_cancellation(
    canceller: Callable[[], None] | None,
    cancel: asyncio.CancelledError,
) -> None:
    if canceller is None:
        return
    try:
        await _await_retry_reset_canceller(canceller)
    except TimeoutError:
        cancel.add_note(
            "Workspace retry reset canceller did not stop within its deadline."
        )
    except Exception as exc:
        cancel.add_note(f"Workspace retry reset cancellation failed: {exc}")


async def _await_cancelled_retry_worker(
    reset_task: asyncio.Task[None],
    cancel: asyncio.CancelledError,
) -> None:
    try:
        await _wait_for_retry_worker(reset_task)
    except TimeoutError:
        reset_task.add_done_callback(_consume_retry_worker_result)
        cancel.add_note("Workspace retry reset did not stop within its deadline.")
    except Exception as exc:
        cancel.add_note(f"Workspace retry reset after cancellation failed: {exc}")


async def _cancel_timed_out_retry_reset(running_reset: _RunningRetryReset) -> None:
    if running_reset.canceller is not None:
        with suppress(Exception):
            await _await_retry_reset_canceller(running_reset.canceller)
    try:
        await _wait_for_retry_worker(running_reset.task)
    except TimeoutError:
        running_reset.task.add_done_callback(_consume_retry_worker_result)
    except Exception:
        return


async def _await_retry_reset_canceller(canceller: Callable[[], None]) -> None:
    task = _daemon_retry_worker_task(canceller)
    try:
        await _wait_for_retry_worker(task)
    except TimeoutError:
        task.add_done_callback(_consume_retry_worker_result)
        raise


async def _wait_for_retry_worker(task: asyncio.Task[None]) -> None:
    await asyncio.wait_for(
        asyncio.shield(task),
        timeout=RETRY_RESET_DEADLINE_SECONDS,
    )


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
    workspace_mutator_fence.fence_workspace_mutator(state_path)
    try:
        workspace_state.mutate_workspace_state(
            state_path,
            lambda payload: payload.__setitem__(
                "workspace_mutator",
                {"status": "unresolved", "operation": "retry_reset"},
            ),
        )
    except BaseException:
        workspace_mutator_fence.release_workspace_mutator(state_path)
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
    try:
        workspace_state.mutate_workspace_state(
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
        workspace_mutator_fence.release_workspace_mutator(state_path)

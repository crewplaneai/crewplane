from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from threading import Thread
from typing import Any


@dataclass
class DeferredAsyncCleanupRegistry:
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    timeout_cancellable_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    protected_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    _closed: bool = False

    def register(
        self,
        cleanup: Coroutine[Any, Any, None],
        cancel_on_timeout: bool = True,
    ) -> None:
        if self._closed:
            if cancel_on_timeout:
                cleanup.close()
                return
            task = asyncio.create_task(cleanup)
            self.protected_tasks.add(task)
            task.add_done_callback(_consume_abandoned_task_result)
            task.add_done_callback(self.protected_tasks.discard)
            return
        task = asyncio.create_task(cleanup)
        self.tasks.add(task)
        if cancel_on_timeout:
            self.timeout_cancellable_tasks.add(task)
        else:
            self.protected_tasks.add(task)

    @property
    def has_unfinished_protected_tasks(self) -> bool:
        return any(not task.done() for task in self.protected_tasks)

    async def drain(self, timeout_seconds: float) -> tuple[Exception, ...]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        errors: list[Exception] = []
        timed_out = False

        while self.tasks:
            tasks = tuple(self.tasks)
            remaining_seconds = deadline - loop.time()
            if remaining_seconds <= 0:
                timed_out = True
                await self._abandon_pending_tasks(tasks)
                break
            done, pending = await asyncio.wait(tasks, timeout=remaining_seconds)
            errors.extend(self._errors_from_done_tasks(done))
            self._discard_tasks(done)
            if pending:
                timed_out = True
                await self._abandon_pending_tasks(pending)
                break
        if timed_out:
            errors.append(
                TimeoutError(
                    "Deferred cleanup did not finish within "
                    f"{timeout_seconds} second(s)."
                )
            )
        return tuple(errors)

    def _discard_tasks(self, tasks: Iterable[asyncio.Task[None]]) -> None:
        for task in tasks:
            self.tasks.discard(task)
            self.timeout_cancellable_tasks.discard(task)
            self.protected_tasks.discard(task)

    async def _abandon_pending_tasks(
        self,
        tasks: Iterable[asyncio.Task[None]],
    ) -> None:
        self._closed = True
        abandoned = tuple(tasks)
        for task in abandoned:
            if task in self.timeout_cancellable_tasks:
                task.cancel()
            task.add_done_callback(_consume_abandoned_task_result)
            self.tasks.discard(task)
            self.timeout_cancellable_tasks.discard(task)
            if task in self.protected_tasks:
                task.add_done_callback(self.protected_tasks.discard)
        # Deliver cancellation without waiting for arbitrary cleanup code to settle.
        await asyncio.sleep(0)

    @staticmethod
    def _errors_from_done_tasks(
        tasks: Iterable[asyncio.Task[None]],
    ) -> tuple[Exception, ...]:
        errors: list[Exception] = []
        for task in tasks:
            if task.cancelled():
                errors.append(RuntimeError("Deferred cleanup task was cancelled."))
                continue
            result = task.exception()
            if result is None:
                continue
            if isinstance(result, Exception):
                errors.append(result)
            else:
                errors.append(RuntimeError(str(result)))
        return tuple(errors)


def _consume_abandoned_task_result(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, RuntimeError):
        return


def workspace_worker_task[**P, Result](
    worker: Callable[P, Result],
    *args: P.args,
    **kwargs: P.kwargs,
) -> asyncio.Task[Result]:
    return asyncio.create_task(_await_workspace_worker(worker, *args, **kwargs))


async def _await_workspace_worker[**P, Result](
    worker: Callable[P, Result],
    *args: P.args,
    **kwargs: P.kwargs,
) -> Result:
    loop = asyncio.get_running_loop()
    completion: asyncio.Future[Result] = loop.create_future()

    def run() -> None:
        try:
            result = worker(*args, **kwargs)
        except BaseException as exc:
            try:
                loop.call_soon_threadsafe(
                    _fail_workspace_worker,
                    completion,
                    exc,
                )
            except RuntimeError:
                return
        else:
            try:
                loop.call_soon_threadsafe(
                    _finish_workspace_worker,
                    completion,
                    result,
                )
            except RuntimeError:
                return

    Thread(target=run, name="crewplane-workspace-worker", daemon=True).start()
    return await completion


def _finish_workspace_worker[Result](
    completion: asyncio.Future[Result],
    result: Result,
) -> None:
    if completion.done():
        return
    completion.set_result(result)


def _fail_workspace_worker[Result](
    completion: asyncio.Future[Result],
    error: BaseException,
) -> None:
    if completion.done():
        return
    completion.set_exception(error)

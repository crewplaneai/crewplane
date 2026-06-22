from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DeferredAsyncCleanupRegistry:
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    timeout_cancellable_tasks: set[asyncio.Task[None]] = field(default_factory=set)

    def register(
        self,
        cleanup: Coroutine[Any, Any, None],
        cancel_on_timeout: bool = True,
    ) -> None:
        task = asyncio.create_task(cleanup)
        self.tasks.add(task)
        if cancel_on_timeout:
            self.timeout_cancellable_tasks.add(task)

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
                errors.extend(await self._cancel_pending_tasks(tasks))
                break
            done, pending = await asyncio.wait(tasks, timeout=remaining_seconds)
            errors.extend(self._errors_from_done_tasks(done))
            self._discard_tasks(done)
            if pending:
                timed_out = True
                errors.extend(await self._cancel_pending_tasks(pending))
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

    def _detach_tasks(self, tasks: Iterable[asyncio.Task[None]]) -> None:
        for task in tasks:
            self._discard_tasks((task,))
            task.add_done_callback(self._report_detached_task_completion)

    async def _cancel_pending_tasks(
        self,
        tasks: Iterable[asyncio.Task[None]],
    ) -> tuple[Exception, ...]:
        errors: list[Exception] = []
        done_tasks = tuple(task for task in tasks if task.done())
        errors.extend(self._errors_from_done_tasks(done_tasks))
        self._discard_tasks(done_tasks)

        pending_tasks = tuple(
            task
            for task in tasks
            if not task.done() and task in self.timeout_cancellable_tasks
        )
        protected_pending_tasks = tuple(
            task
            for task in tasks
            if not task.done() and task not in self.timeout_cancellable_tasks
        )
        self._detach_tasks(protected_pending_tasks)
        if not pending_tasks:
            return tuple(errors)
        for task in pending_tasks:
            task.cancel()
        results = await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._discard_tasks(pending_tasks)
        errors.extend(self._errors_from_cancel_results(results))
        return tuple(errors)

    @staticmethod
    def _report_detached_task_completion(task: asyncio.Task[None]) -> None:
        try:
            result = task.exception()
        except asyncio.CancelledError:
            result = RuntimeError("Detached deferred cleanup task was cancelled.")
        if result is None:
            return
        error = result if isinstance(result, Exception) else RuntimeError(str(result))
        task.get_loop().call_exception_handler(
            {
                "message": "Detached deferred cleanup task failed after drain timeout.",
                "exception": error,
                "task": task,
            }
        )

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

    @staticmethod
    def _errors_from_cancel_results(results: Iterable[object]) -> tuple[Exception, ...]:
        errors: list[Exception] = []
        for result in results:
            if result is None or isinstance(result, asyncio.CancelledError):
                continue
            if isinstance(result, Exception):
                errors.append(result)
            elif isinstance(result, BaseException):
                errors.append(RuntimeError(str(result)))
        return tuple(errors)

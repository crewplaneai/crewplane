from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from threading import Event

from crewplane.architecture.contracts import (
    InvocationContext,
)
from crewplane.core.config import AgentConfig


class SlowSuccessfulWorkspace:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.finished = Event()
        self.child_environment_applied: bool | None = None
        self.defer_cleanup: bool | None = None
        self.cancel_requested_check: Callable[[], bool] | None = None
        self.cancel_requested: bool | None = None

    def mark_succeeded(
        self,
        child_environment_applied: bool | None = None,
        defer_cleanup: bool = False,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        self.child_environment_applied = child_environment_applied
        self.defer_cleanup = defer_cleanup
        self.cancel_requested_check = cancel_requested
        self.started.set()
        assert self.release.wait(2)
        self.cancel_requested = (
            cancel_requested() if cancel_requested is not None else None
        )
        self.finished.set()


async def wait_for_workspace_cancellation(
    cancel_requested: Callable[[], bool] | None,
) -> None:
    assert cancel_requested is not None
    async with asyncio.timeout(2):
        while not cancel_requested():
            await asyncio.sleep(0)


class SlowSuccessfulPreparedWorkspace(SlowSuccessfulWorkspace):
    def __init__(self, workspace_path: Path, state_path: Path | None = None) -> None:
        super().__init__()
        self.workspace_path = workspace_path
        self.workspace_path.mkdir()
        self.state_path = state_path
        self.cleanup_on_success = True
        self.cleaned = False
        self.cleaned_event = Event()

    def cleanup_after_success(self) -> None:
        self.cleaned = True
        self.cleaned_event.set()


class SuccessfulRuntimeInvoker:
    async def invoke(
        self,
        config: AgentConfig,
        model: str | None,
        prompt: str,
        output_file: Path,
        cwd: Path,
        log_file: Path | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> None:
        del config, model, prompt, cwd, log_file, invocation_context
        output_file.write_text("done\n", encoding="utf-8")

    def log_presentation_for(self, config: AgentConfig) -> None:
        del config
        return None


def exception_notes_contain(exc: BaseException, expected: str) -> bool:
    return any(expected in note for note in getattr(exc, "__notes__", ()))

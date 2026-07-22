from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest

import crewplane.runtime.execution.provider_call.workspace as workspace_module
from crewplane.runtime.execution.runtime_context import DeferredAsyncCleanupRegistry
from crewplane.runtime.workspace import PreparedWorkspace
from crewplane.runtime.workspace.setup import WorkspaceSetupCancellation
from tests.helpers.workspace_service import (
    disabled_workspace_plan,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
)


def test_late_workspace_preparation_result_is_marked_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_late_workspace_preparation_result_is_marked_cancelled(
            tmp_path,
            monkeypatch,
        )
    )


async def _run_late_workspace_preparation_result_is_marked_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    invocation_context = workspace_invocation_context()
    prepared_workspace = PreparedWorkspace(repo, invocation_context)
    prepare_workspace = Mock(return_value=prepared_workspace)
    mark_cancelled = Mock()
    cancellation_checked = Event()
    allow_preparation_to_return = Event()
    original_is_cancelled = WorkspaceSetupCancellation.is_cancelled

    def pause_after_cancellation_check(
        setup_cancellation: WorkspaceSetupCancellation,
    ) -> bool:
        cancellation_requested = original_is_cancelled(setup_cancellation)
        if not cancellation_requested:
            cancellation_checked.set()
            if not allow_preparation_to_return.wait(timeout=2):
                raise TimeoutError("Test did not release workspace preparation.")
        return cancellation_requested

    monkeypatch.setattr(
        workspace_module,
        "PREPARATION_CANCELLATION_TIMEOUT_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        workspace_module,
        "prepare_invocation_workspace",
        prepare_workspace,
    )
    monkeypatch.setattr(
        WorkspaceSetupCancellation,
        "is_cancelled",
        pause_after_cancellation_check,
    )
    monkeypatch.setattr(prepared_workspace, "mark_cancelled", mark_cancelled)
    request = workspace_invocation_request(
        disabled_workspace_plan(repo),
        workspace_output_manager(tmp_path, repo),
    )
    cleanup_registry = DeferredAsyncCleanupRegistry()
    preparation_task = asyncio.create_task(
        workspace_module.prepare_workspace_with_cancellation(
            request,
            invocation_context,
            cleanup_registry,
        )
    )

    try:
        assert await asyncio.to_thread(cancellation_checked.wait, 2)
        preparation_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await preparation_task
    finally:
        allow_preparation_to_return.set()

    cleanup_errors = await cleanup_registry.drain(2.0)

    assert cleanup_errors == ()
    prepare_workspace.assert_called_once()
    mark_cancelled.assert_called_once_with(
        workspace_module.PREPARATION_CANCELLATION_MESSAGE,
        None,
    )

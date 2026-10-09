from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from crewplane.runtime.execution.provider_call.lifecycle_state import (
    ProviderInvocationLifecycleState,
)
from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace.prepared_workspace import PreparedWorkspace
from tests.helpers.platforms import requires_workspace_support
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)

pytestmark = requires_workspace_support


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "running"])
def test_cancellation_preserves_terminal_state_and_terminalizes_running(
    tmp_path, status
):
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo, tmp_path / "cache", cleanup_on_success=True, kind="snapshot"
    )
    output = workspace_output_manager(tmp_path, repo)
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output), workspace_invocation_context()
    )
    assert prepared.state_path is not None
    state = read_json_object(prepared.state_path)
    state["status"] = status
    prepared.state_path.write_text(json.dumps(state), encoding="utf-8")
    before = prepared.state_path.read_bytes()
    lifecycle = ProviderInvocationLifecycleState(prepared_workspace=prepared)

    asyncio.run(lifecycle.mark_cancelled(asyncio.CancelledError()))

    if status == "running":
        assert read_json_object(prepared.state_path)["status"] == "cancelled"
    else:
        assert prepared.state_path.read_bytes() == before
        assert prepared.workspace_path.is_dir()


@pytest.mark.parametrize(
    "state", ["missing", "symlink", "unreadable", "malformed", "non-mapping", "no-path"]
)
def test_cancellation_delegates_when_terminal_evidence_is_unavailable(tmp_path, state):
    state_path = tmp_path / "workspace-state.json"
    if state == "symlink":
        target = tmp_path / "target.json"
        target.write_text('{"status": "succeeded"}', encoding="utf-8")
        state_path.symlink_to(target)
    elif state == "unreadable":
        state_path.write_text('{"status": "succeeded"}', encoding="utf-8")
    elif state == "malformed":
        state_path.write_text("{", encoding="utf-8")
    elif state == "non-mapping":
        state_path.write_text("[]", encoding="utf-8")
    prepared = PreparedWorkspace(
        cwd=tmp_path,
        invocation_context=workspace_invocation_context(),
        state_path=None if state == "no-path" else state_path,
    )
    lifecycle = ProviderInvocationLifecycleState(prepared_workspace=prepared)
    cancelled = Mock()
    with patch.object(PreparedWorkspace, "mark_cancelled", new=cancelled):
        if state == "unreadable":
            with patch.object(Path, "read_text", side_effect=PermissionError):
                asyncio.run(lifecycle.mark_cancelled(asyncio.CancelledError()))
        else:
            asyncio.run(lifecycle.mark_cancelled(asyncio.CancelledError()))
    cancelled.assert_called_once_with("Provider invocation was cancelled.", None)

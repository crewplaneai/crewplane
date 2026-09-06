from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest

from crewplane.architecture.contracts import (
    InvocationSourceContext,
    InvocationWorkspaceContext,
    InvocationWorktreeContract,
)
from crewplane.runtime.agent.invocation.retry_reset import reset_before_retry
from crewplane.runtime.workspace.mutator_fence import workspace_mutator_is_fenced
from tests.helpers.workspace_service import workspace_invocation_context


def test_retry_reset_write_failure_aborts_before_worker_launch(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "workspace-state.json"
    state_path.write_text("{}", encoding="utf-8")
    reset_started = Event()
    base = workspace_invocation_context()
    workspace = InvocationWorkspaceContext(
        workspace_kind="worktree",
        materialization="worktree_checkout",
        logical_worktree_name="primary",
        cwd=tmp_path,
        invocation_source=InvocationSourceContext(
            source_kind="project",
            source_node_id=None,
            source_commit="a" * 40,
            source_tree="b" * 40,
        ),
        worktree_contract=InvocationWorktreeContract(
            mode="blob_exact", schema_version="1.0"
        ),
        workspace_state_path=state_path,
    )
    context = replace(
        base,
        retry_reset=reset_started.set,
        workspace=workspace,
    )

    async def exercise() -> None:
        with (
            patch(
                "crewplane.runtime.workspace.state.mutate_workspace_state",
                side_effect=OSError("injected state write failure"),
            ),
            pytest.raises(OSError, match="injected state write failure"),
        ):
            await reset_before_retry(context)
        assert not reset_started.is_set()
        assert not workspace_mutator_is_fenced(state_path)
        assert state_path.read_text(encoding="utf-8") == "{}"

    asyncio.run(exercise())


def test_retry_reset_deadline_does_not_block_asyncio_run_shutdown() -> None:
    script = textwrap.dedent(
        """
        import asyncio
        from threading import Event

        from crewplane.architecture.contracts import InvocationContext
        from crewplane.core.workflow.keywords import ProviderRole
        from crewplane.runtime.agent.invocation import retry_reset

        blocker = Event()
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="provider",
            role=ProviderRole.EXECUTOR,
            retry_reset=blocker.wait,
            retry_reset_canceller=blocker.wait,
        )
        retry_reset.RETRY_RESET_DEADLINE_SECONDS = 0.01

        async def run():
            try:
                await retry_reset.reset_before_retry(context)
            except RuntimeError:
                return
            raise AssertionError("retry reset unexpectedly completed")

        asyncio.run(run())
        print("asyncio-run-returned", flush=True)
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=2.0,
    )

    assert result.stdout.strip() == "asyncio-run-returned"

from __future__ import annotations

import asyncio
import sys
from typing import cast

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.artifacts.manager import OutputManager
from crewplane.core.provider_process_state import ProviderProcessState
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.invocation.command import run_command_once
from crewplane.runtime.execution.activity.events import InvocationMetadata
from crewplane.runtime.execution.provider_call.display import ProviderCallDisplay
from crewplane.runtime.execution.provider_call.events import build_invocation_context


class _LegacyArtifactStore:
    """Artifact store shape from before process receipts were optional."""


def test_initial_reviewer_round_persists_provider_process_lifecycle(tmp_path) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    context, _ = build_invocation_context(
        telemetry=None,
        metadata=InvocationMetadata(
            node_id="build.node",
            provider="codex",
            role=ProviderRole.REVIEWER,
            model="gpt-5",
            task_id="codex_reviewer_0",
            audit_round_num=None,
            round_num=0,
            output_file=tmp_path / "output.md",
            log_file=None,
        ),
        display=ProviderCallDisplay(telemetry=None),
        output=output,
    )
    assert context.process_event_sink is not None

    result = asyncio.run(
        run_command_once(
            cmd=[sys.executable, "-c", "print('review complete')"],
            stdin_data=None,
            log_file=None,
            append_log=False,
            log_header=None,
            cwd=tmp_path,
            invocation_context=context,
            idle_timeout_seconds=None,
        )
    )
    result.cleanup_stream_files()

    process_states = tuple(
        (output.stages_dir / "manifests" / "provider-processes").glob("*.json")
    )
    assert len(process_states) == 1
    state = ProviderProcessState.model_validate_json(
        process_states[0].read_text(encoding="utf-8")
    )
    assert state.status == "exited"
    assert state.node_id == "build.node"
    assert state.task_id == "codex_reviewer_0"
    assert state.round_num == 0


def test_legacy_artifact_store_keeps_cli_invocation_working(tmp_path) -> None:
    context, _ = build_invocation_context(
        telemetry=None,
        metadata=InvocationMetadata(
            node_id="build.node",
            provider="generic",
            role=ProviderRole.EXECUTOR,
            model=None,
            task_id="generic_executor_0",
            audit_round_num=None,
            round_num=1,
            output_file=tmp_path / "output.md",
            log_file=None,
        ),
        display=ProviderCallDisplay(telemetry=None),
        output=cast(ArtifactStorePort, _LegacyArtifactStore()),
    )

    assert context.process_event_sink is None
    result = asyncio.run(
        run_command_once(
            cmd=[sys.executable, "-c", "print('provider complete')"],
            stdin_data=None,
            log_file=None,
            append_log=False,
            log_header=None,
            cwd=tmp_path,
            invocation_context=context,
            idle_timeout_seconds=None,
        )
    )
    try:
        assert result.returncode == 0
    finally:
        result.cleanup_stream_files()

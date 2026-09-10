import json
import tempfile
from pathlib import Path

import pytest

from crewplane.architecture.contracts import EventType
from crewplane.artifacts import OutputManager
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability import PersistentRunLogger
from crewplane.observability.persistent import (
    render_run_summary_terminal,
)
from crewplane.observability.run_summary.logger import (
    MAX_RETAINED_SUMMARY_EVENTS,
)
from crewplane.observability.run_summary.models import (
    WorkspaceInvocationExecutionSummary,
    WorkspaceInvocationSummary,
)
from crewplane.observability.run_summary.workspace import (
    merge_workspace_invocations,
    workspace_state_summaries,
)
from crewplane.observability.types import (
    RunContext,
    RunResult,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.observability import (
    make_execution_event,
    topology_from_workflow,
)
from tests.integration.observability.runtime.observability_runtime_helpers import (
    single_node_workflow,
)


def test_persistent_run_summary_includes_workspace_observability() -> None:
    workflow = single_node_workflow()
    with tempfile.TemporaryDirectory() as tmp_dir:
        output = OutputManager(workflow.name, base_dir=Path(tmp_dir))
        output.write_preflight_manifest(
            {
                "workspace": {
                    "worktree_contract": {
                        "mode": "blob_exact",
                        "schema_version": SCHEMA_VERSION,
                    },
                    "source": {
                        "run_base_commit": "a" * 40,
                        "source_tree": "b" * 40,
                        "object_format": "sha1",
                        "clean_start": "tracked_only",
                    },
                    "invoker": {
                        "implementation": "cli",
                        "launch_mode": "runtime_command_runner",
                        "controlled_child_environment": True,
                    },
                    "rendered_files": {
                        "locator_count": 2,
                        "project_initial": 1,
                        "runtime_dynamic": 1,
                    },
                    "cleanup": {
                        "cleanup_on_success": True,
                        "cache_root_configured": True,
                    },
                    "nodes": [{"node_id": "node.a"}],
                }
            }
        )
        stage_dir = output.create_node_dir(node_artifact_request("node.a"))
        state_path = stage_dir / "workspace-state.json"
        state_path.write_text(
            json.dumps(
                {
                    "version": SCHEMA_VERSION,
                    "node_id": "node.a",
                    "task_id": "alpha_executor_0",
                    "round_num": 1,
                    "status": "succeeded",
                    "workspace_kind": "worktree",
                    "logical_worktree_name": "implementation",
                    "worktree_contract": {
                        "mode": "blob_exact",
                        "schema_version": SCHEMA_VERSION,
                    },
                    "invocation_source": {
                        "source_kind": "node",
                        "source_node_id": "implement",
                        "source_commit": "a" * 40,
                        "source_tree": "b" * 40,
                    },
                    "workspace": {
                        "materialization": "worktree_checkout",
                        "writable": True,
                        "lineage_producer": True,
                        "retention": "deleted",
                    },
                    "execution": {
                        "cache_root": (Path(tmp_dir) / "cache").as_posix(),
                        "workspace_path": (
                            Path(tmp_dir) / "cache/workspaces/repo/run/impl"
                        ).as_posix(),
                        "checkout_root": (
                            Path(tmp_dir) / "cache/workspaces/repo/run/impl/checkout"
                        ).as_posix(),
                        "effective_cwd": (
                            Path(tmp_dir) / "cache/workspaces/repo/run/impl/checkout"
                        ).as_posix(),
                        "checkout_size_bytes": 2048,
                        "provisioning_duration_seconds": 0.125,
                    },
                    "child_process_environment": {
                        "required": True,
                        "applied": True,
                    },
                    "setup": {
                        "profile_name": "node_dependencies",
                        "status": "succeeded",
                        "duration_seconds": 1.25,
                        "commands": [
                            {
                                "argv": ["pnpm", "install", "--frozen-lockfile"],
                                "exit_code": 0,
                            }
                        ],
                        "log_path": "workspace-setup/setup.log",
                        "metadata_path": "workspace-setup/setup.json",
                    },
                    "reuse": {
                        "strategy": "incremental_reset",
                        "reused": True,
                        "fallback": False,
                        "previous_workspace_state": "workspace-state.json",
                    },
                    "rendered_workspace_files": [
                        {
                            "occurrence_id": "node.a:executor:0:file:README.md",
                            "injected_sha256": "e" * 64,
                        }
                    ],
                    "result": {
                        "candidate_commit": "c" * 40,
                        "result_commit": "d" * 40,
                        "result_tree": "e" * 40,
                        "changed_path_count": 2,
                        "final_head": "a" * 40,
                    },
                    "bundle": {
                        "path": "node.a/workspace-bundles/alpha.bundle",
                        "size_bytes": 123,
                        "verified": True,
                    },
                    "branch_export": {
                        "status": "skipped",
                        "operation": "skipped",
                        "branch_name": None,
                        "branch_ref": None,
                        "record_artifact": "workspace-exports/implementation.json",
                        "skip_reason": "create_branch_false",
                        "completed_at": "2026-06-16T12:00:00+00:00",
                    },
                }
            ),
            encoding="utf-8",
        )
        snapshot_stage_dir = output.create_node_dir(
            node_artifact_request("snapshot.node")
        )
        (snapshot_stage_dir / "workspace-state.json").write_text(
            json.dumps(
                {
                    "version": SCHEMA_VERSION,
                    "node_id": "snapshot.node",
                    "task_id": "alpha_reviewer_0",
                    "round_num": 1,
                    "status": "succeeded",
                    "workspace_kind": "snapshot",
                    "logical_worktree_name": "review_snapshot",
                    "worktree_contract": {
                        "mode": "blob_exact",
                        "schema_version": SCHEMA_VERSION,
                    },
                    "invocation_source": {
                        "source_kind": "project",
                        "source_commit": "a" * 40,
                        "source_tree": "b" * 40,
                    },
                    "workspace": {
                        "materialization": "snapshot_checkout",
                        "writable": True,
                        "lineage_producer": False,
                        "retention": "deleted",
                    },
                    "execution": {
                        "cache_root": (Path(tmp_dir) / "cache").as_posix(),
                        "workspace_path": (
                            Path(tmp_dir) / "cache/snapshots/repo/run/review"
                        ).as_posix(),
                        "checkout_root": (
                            Path(tmp_dir) / "cache/snapshots/repo/run/review/checkout"
                        ).as_posix(),
                        "effective_cwd": (
                            Path(tmp_dir) / "cache/snapshots/repo/run/review/checkout"
                        ).as_posix(),
                        "checkout_size_bytes": 2048,
                        "provisioning_duration_seconds": 0.05,
                    },
                    "result": {
                        "lineage_produced": False,
                        "snapshot_drift_discarded": True,
                        "changed_path_count": 2,
                        "changed_paths": ["coverage.xml", "tmp/cache"],
                        "changed_paths_truncated": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        persistent_logger = PersistentRunLogger(output)
        persistent_logger.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id=output.run_id,
                refresh_per_second=0,
            )
        )
        persistent_logger.record_event(
            make_execution_event(
                event_type=EventType.INVOCATION_FINISHED,
                workflow_name=workflow.name,
                run_id=output.run_id,
                node_id="node.a",
                provider="alpha",
                role=ProviderRole.EXECUTOR,
                task_id="alpha_executor_0",
                round_num=1,
            )
        )
        persistent_logger.record_event(
            make_execution_event(
                event_type=EventType.WORKSPACE_CONTEXT_RECORDED,
                workflow_name=workflow.name,
                run_id=output.run_id,
                node_id="node.a",
                provider="alpha",
                role=ProviderRole.EXECUTOR,
                task_id="alpha_executor_0",
                round_num=1,
                status="succeeded",
                workspace_kind="worktree",
                workspace_logical_worktree_name="implementation",
                workspace_materialization="worktree_checkout",
                workspace_source_kind="project",
                workspace_source_commit="a" * 40,
                workspace_source_tree="b" * 40,
                worktree_contract_mode="blob_exact",
                worktree_contract_schema_version=SCHEMA_VERSION,
                workspace_state_path=str(state_path),
                workspace_writable=True,
                workspace_lineage_producer=True,
                workspace_child_environment_required=True,
                workspace_child_environment_applied=True,
            )
        )

        persistent_logger.stop(RunResult(status="succeeded"))

        summary = persistent_logger.last_summary
        assert summary is not None
        assert summary is not None
        assert summary.workspace is not None
        assert summary.workspace is not None
        assert summary.workspace.plan is not None
        assert summary.workspace.plan is not None
        assert summary.workspace.plan.planned_workspace_node_count == 1
        assert len(summary.workspace.invocations) == 2
        worktree_invocation = next(
            invocation
            for invocation in summary.workspace.invocations
            if invocation.workspace_kind == "worktree"
        )
        snapshot_invocation = next(
            invocation
            for invocation in summary.workspace.invocations
            if invocation.workspace_kind == "snapshot"
        )
        assert worktree_invocation.setup.status == "succeeded"
        assert worktree_invocation.setup.duration_seconds == 1.25
        assert worktree_invocation.reuse.strategy == "incremental_reset"
        assert worktree_invocation.reuse.reused
        assert worktree_invocation.reuse.reset_verification == "verified"
        assert worktree_invocation.execution.checkout_size_bytes == 2048
        assert worktree_invocation.branch_export.operation == "skipped"
        assert worktree_invocation.checkpoint_count == 1
        assert snapshot_invocation.snapshot_drift_discarded
        assert snapshot_invocation.snapshot_changed_paths_reported == 2
        summary_text = output.get_run_summary_path().read_text(encoding="utf-8")
        assert "## Workspace Observability" in summary_text
        assert f"contract=blob_exact:{SCHEMA_VERSION}" in summary_text
        assert "rendered workspace files=2" in summary_text
        assert "launch=runtime_command_runner" in summary_text
        assert "cleanup_on_success=yes" in summary_text
        assert "rendered_files=1" in summary_text
        assert "result=" + "d" * 40 in summary_text
        assert "bundle=node.a/workspace-bundles/alpha.bundle" in summary_text
        assert "checkpoints=1" in summary_text
        assert "setup=node_dependencies, status=succeeded" in summary_text
        assert "duration=1.250s" in summary_text
        assert "reuse=incremental_reset, reused=yes" in summary_text
        assert "reset=verified" in summary_text
        assert "execution=cache_root=" in summary_text
        assert "checkout_bytes=2048" in summary_text
        assert "branch_export=status=skipped" in summary_text
        assert "operation=skipped" in summary_text
        assert "snapshot_drift=discarded=yes, changes=2" in summary_text
        terminal_summary = render_run_summary_terminal(summary)
        assert "Workspace Observability" in terminal_summary
        assert "launch=runtime_command_runner" in terminal_summary
        assert "rendered_files=1" in terminal_summary
        assert "setup=node_dependencies:succeeded" in terminal_summary
        assert "reuse=incremental_reset,reused=True" in terminal_summary
        assert "reset=verified" in terminal_summary
        assert "branch_export=skipped,operation=skipped" in terminal_summary


def test_workspace_summary_terminal_event_status_wins_over_stale_state(
    tmp_path: Path,
) -> None:
    stages_dir = tmp_path
    stage_dir = stages_dir / "node.a"
    stage_dir.mkdir()
    state_path = stage_dir / "workspace-state.json"
    state_invocation = _workspace_summary(
        status="running",
        state_path="node.a/workspace-state.json",
        execution=WorkspaceInvocationExecutionSummary(
            cache_root="/tmp/crewplane-cache",
        ),
    )
    event_invocation = _workspace_summary(
        status="failed",
        state_path=state_path.as_posix(),
        writable=True,
        lineage_producer=True,
    )

    merged = merge_workspace_invocations(
        stages_dir,
        (event_invocation,),
        (state_invocation,),
    )

    assert len(merged) == 1
    assert merged[0].status == "failed"
    assert merged[0].state_path == "node.a/workspace-state.json"
    assert merged[0].writable
    assert merged[0].lineage_producer
    assert merged[0].execution.cache_root == "/tmp/crewplane-cache"


def test_workspace_summary_terminal_state_status_wins_over_running_event(
    tmp_path: Path,
) -> None:
    stages_dir = tmp_path
    stage_dir = stages_dir / "node.a"
    stage_dir.mkdir()
    state_path = stage_dir / "workspace-state.json"
    state_invocation = _workspace_summary(
        status="cancelled",
        state_path="node.a/workspace-state.json",
        execution=WorkspaceInvocationExecutionSummary(
            cache_root="/tmp/crewplane-cache",
        ),
    )
    event_invocation = _workspace_summary(
        status="running",
        state_path=state_path.as_posix(),
        writable=True,
        lineage_producer=True,
    )

    merged = merge_workspace_invocations(
        stages_dir,
        (event_invocation,),
        (state_invocation,),
    )

    assert len(merged) == 1
    assert merged[0].status == "cancelled"
    assert merged[0].state_path == "node.a/workspace-state.json"
    assert merged[0].writable
    assert merged[0].lineage_producer
    assert merged[0].execution.cache_root == "/tmp/crewplane-cache"


def test_workspace_summary_skips_non_utf8_workspace_state(tmp_path: Path) -> None:
    stages_dir = tmp_path
    stage_dir = stages_dir / "node.a"
    stage_dir.mkdir()
    (stage_dir / "workspace-state.json").write_bytes(b"\xff")

    summaries = workspace_state_summaries(stages_dir)

    assert summaries == ()


def test_workspace_summary_uses_canonical_filenames_in_lexical_order(
    subtests: pytest.Subtests,
) -> None:
    for canonical_exists in (False, True):
        with (
            subtests.test(canonical_exists=canonical_exists),
            tempfile.TemporaryDirectory() as tmp_dir,
        ):
            stages_dir = Path(tmp_dir)
            stage_dir = stages_dir / "node.a"
            stage_dir.mkdir()
            names = ["workspace-state-z.json", "workspace-state-a.json"]
            if canonical_exists:
                names.append("workspace-state.json")
            payload = json.dumps(_workspace_state_summary_payload("node.a"))
            for name in names:
                (stage_dir / name).write_text(payload, encoding="utf-8")
            for name in (
                "workspace-stateful.json",
                "workspace-reuse-claim-a.json",
            ):
                (stage_dir / name).write_text(payload, encoding="utf-8")

            summaries = workspace_state_summaries(stages_dir)

            assert [summary.state_path for summary in summaries] == [
                f"node.a/{name}" for name in sorted(names)
            ]


def test_workspace_summary_rejects_unsafe_or_invalid_state_files(
    tmp_path: Path,
) -> None:
    stages_dir = tmp_path / "stages"
    assert workspace_state_summaries(stages_dir) == ()
    stage_dir = stages_dir / "node.a"
    stage_dir.mkdir(parents=True)
    target = tmp_path / "target.json"
    target.write_text(
        json.dumps(_workspace_state_summary_payload("node.a")),
        encoding="utf-8",
    )
    (stage_dir / "workspace-state-link.json").symlink_to(target)
    (stage_dir / "workspace-state-directory.json").mkdir()
    (stage_dir / "workspace-state-malformed.json").write_text("{")
    outside_stage = tmp_path / "outside-stage"
    outside_stage.mkdir()
    (outside_stage / "workspace-state.json").write_text(
        json.dumps(_workspace_state_summary_payload("node.b")),
        encoding="utf-8",
    )
    (stages_dir / "node.b").symlink_to(outside_stage, target_is_directory=True)

    assert workspace_state_summaries(stages_dir) == ()


def test_workspace_summary_ignores_non_node_stage_workspace_states(
    tmp_path: Path,
) -> None:
    stages_dir = tmp_path
    stage_dir = stages_dir / "node.a"
    nested_dir = stage_dir / "generated-file-sources" / "snapshot"
    nested_dir.mkdir(parents=True)
    (nested_dir / "workspace-state.json").write_text(
        json.dumps(_workspace_state_summary_payload("node.a")),
        encoding="utf-8",
    )
    (stage_dir / "workspace-state.json").write_text(
        json.dumps(_workspace_state_summary_payload("other.node")),
        encoding="utf-8",
    )

    summaries = workspace_state_summaries(stages_dir)

    assert summaries == ()


def test_workspace_summary_survives_retained_event_detail_cap() -> None:
    workflow = single_node_workflow()
    overflow_count = 3
    with tempfile.TemporaryDirectory() as tmp_dir:
        output = OutputManager(workflow.name, base_dir=Path(tmp_dir))
        persistent_logger = PersistentRunLogger(output)
        persistent_logger.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id=output.run_id,
                refresh_per_second=0,
            )
        )
        persistent_logger.record_event(
            make_execution_event(
                event_type=EventType.INVOCATION_STARTED,
                workflow_name=workflow.name,
                run_id=output.run_id,
                node_id="node.a",
                provider="alpha",
                role=ProviderRole.EXECUTOR,
                task_id="alpha_executor_0",
            )
        )
        persistent_logger.record_event(
            make_execution_event(
                event_type=EventType.WORKSPACE_CONTEXT_RECORDED,
                workflow_name=workflow.name,
                run_id=output.run_id,
                node_id="node.a",
                provider="alpha",
                role=ProviderRole.EXECUTOR,
                task_id="alpha_executor_0",
                status="running",
                workspace_kind="snapshot",
                workspace_logical_worktree_name="scratch",
                workspace_materialization="snapshot_checkout",
                workspace_source_kind="project",
                workspace_source_commit="a" * 40,
                workspace_source_tree="b" * 40,
                worktree_contract_mode="blob_exact",
                worktree_contract_schema_version=SCHEMA_VERSION,
                workspace_writable=True,
                workspace_lineage_producer=False,
            )
        )
        for index in range(MAX_RETAINED_SUMMARY_EVENTS + overflow_count):
            persistent_logger.record_event(
                make_execution_event(
                    event_type=EventType.RUNTIME_LOG,
                    workflow_name=workflow.name,
                    run_id=output.run_id,
                    level="warning",
                    message=f"summary warning {index}",
                    operation="summary_retention_test",
                )
            )

        persistent_logger.stop(RunResult(status="succeeded"))

        summary = persistent_logger.last_summary
        assert summary is not None
        assert summary is not None
        assert summary.workspace is not None
        assert summary.workspace is not None
        assert summary.workspace.invocations[0].workspace_kind == "snapshot"
        assert summary.workspace.invocations[0].source.commit == "a" * 40


def _workspace_summary(
    status: str,
    state_path: str,
    execution: WorkspaceInvocationExecutionSummary | None = None,
    writable: bool | None = None,
    lineage_producer: bool | None = None,
) -> WorkspaceInvocationSummary:
    return WorkspaceInvocationSummary(
        node_id="node.a",
        task_id="alpha_executor_0",
        audit_round_num=None,
        round_num=1,
        workspace_kind="worktree",
        logical_worktree_name="implementation",
        status=status,
        state_path=state_path,
        writable=writable,
        lineage_producer=lineage_producer,
        child_environment_required=None,
        child_environment_applied=None,
        execution=execution or WorkspaceInvocationExecutionSummary(),
    )


def _workspace_state_summary_payload(node_id: str) -> dict[str, object]:
    return {
        "version": SCHEMA_VERSION,
        "node_id": node_id,
        "task_id": "alpha_executor_0",
        "round_num": 1,
        "status": "succeeded",
        "workspace_kind": "worktree",
    }

import json
import tempfile
import unittest
from pathlib import Path

from crewplane.artifacts import OutputManager
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability import PersistentRunLogger
from crewplane.observability.events import (
    apply_event,
    build_initial_state,
)
from crewplane.observability.persistent import render_run_summary_terminal
from crewplane.observability.run_summary.accumulator import (
    MAX_RETAINED_INVOCATION_USAGE_DETAILS,
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
from crewplane.observability.runtime import ObservabilityHub
from crewplane.observability.types import (
    RunContext,
    RunResult,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.observability import (
    make_execution_event,
    topology_from_workflow,
)
from tests.integration.observability.runtime.observability_runtime_helpers import (
    RecordingObserver,
    single_node_workflow,
)


class PersistentRunLoggerSummaryTests(unittest.TestCase):
    def test_runtime_log_warning_updates_recent_events(self) -> None:
        workflow = single_node_workflow()
        state = build_initial_state(topology_from_workflow(workflow), run_id="run-1")

        apply_event(
            state,
            make_execution_event(
                event_type="runtime_log",
                workflow_name=workflow.name,
                run_id="run-1",
                node_id="node.a",
                level="warning",
                message="used stderr as output",
                operation="stderr_fallback",
            ),
        )

        self.assertIn(
            "WARN used stderr as output", list(state.nodes["node.a"].recent_events)
        )

    def test_persistent_run_logger_writes_ndjson_and_summary(self) -> None:
        workflow = single_node_workflow()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output = OutputManager(
                workflow.name, base_dir=tmp_path, log_cli_output=True
            )
            stage_dir = output.create_stage_dir("node.a")
            output_file = stage_dir / "alpha_executor_0_round1.md"
            log_file = output.get_log_file(
                stage_name="node.a",
                provider="alpha",
                task_id="alpha_executor_0",
                round_num=1,
            )
            assert log_file is not None
            persistent_logger = PersistentRunLogger(output)
            observer = RecordingObserver()

            with ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id=output.run_id,
                observers=[observer, persistent_logger],
                refresh_per_second=0,
            ) as hub:
                hub.emit(
                    make_execution_event(
                        event_type="workflow_started",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="node_started",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="node.a",
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="invocation_started",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="node.a",
                        provider="alpha",
                        role=ProviderRole.EXECUTOR,
                        model="alpha-model",
                        task_id="alpha_executor_0",
                        round_num=1,
                        output_file=str(output_file),
                        log_file=str(log_file),
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="runtime_log",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="node.a",
                        task_id="alpha_executor_0",
                        level="warning",
                        message="stdout was empty; used stderr as output",
                        operation="stderr_fallback",
                        output_file=str(output_file),
                        log_file=str(log_file),
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="invocation_finished",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="node.a",
                        provider="alpha",
                        role=ProviderRole.EXECUTOR,
                        model="alpha-model",
                        task_id="alpha_executor_0",
                        round_num=1,
                        duration_ms=250,
                        attempt_count=2,
                        cli_captured=True,
                        output_extraction_status="success",
                        provider_usage_status="full",
                        provider_tokens={
                            "input": 90,
                            "cached_input": None,
                            "cache_write": None,
                            "output": 12,
                            "reasoning": None,
                            "total": None,
                        },
                        visible_estimate_tokens=42,
                        visible_estimate_method="char-count-lower-bound",
                        visible_estimate_is_lower_bound=True,
                        configured_cost_usd=0.000207,
                        invocation_cost_confidence="full",
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="node_finished",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="node.a",
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="workflow_finished",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                    )
                )

            event_log = output.get_run_event_log_path()
            summary_log = output.get_run_summary_path()
            self.assertTrue(event_log.exists())
            self.assertTrue(summary_log.exists())

            event_log_text = event_log.read_text(encoding="utf-8")
            summary_text = summary_log.read_text(encoding="utf-8")
            self.assertIn('"event_type": "runtime_log"', event_log_text)
            self.assertIn('"operation": "stderr_fallback"', event_log_text)
            self.assertIn(
                "Invocation succeeded with empty stdout; used stderr as output.",
                summary_text,
            )
            self.assertIn(
                "Provider log contains the original stderr lines",
                summary_text,
            )
            self.assertIn('"attempt_count": 2', event_log_text)
            self.assertIn(
                '"provider_tokens": {"cache_write": null, "cached_input": null, "input": 90',
                event_log_text,
            )
            self.assertIn("## Spend Observability", summary_text)
            self.assertIn("provider report: full", summary_text)
            self.assertIn(
                "Configured cost estimate: $0.000207 (confidence: full)", summary_text
            )
            self.assertIn("## Node Outcomes", summary_text)
            self.assertIn("## Artifact References", summary_text)
            last_summary = persistent_logger.last_summary
            self.assertIsNotNone(last_summary)
            assert last_summary is not None
            terminal_summary = render_run_summary_terminal(last_summary)
            self.assertIn(
                "Provider token reports: 1/1 full, 0/1 partial, 0/1 malformed",
                terminal_summary,
            )
            self.assertIn("alpha: 1 invocation(s)", terminal_summary)
            self.assertIn(
                "Configured cost estimate: $0.000207 (full)", terminal_summary
            )

    def test_persistent_run_summary_includes_workspace_observability(self) -> None:
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
            stage_dir = output.create_stage_dir("node.a")
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
                                Path(tmp_dir)
                                / "cache/workspaces/repo/run/impl/checkout"
                            ).as_posix(),
                            "effective_cwd": (
                                Path(tmp_dir)
                                / "cache/workspaces/repo/run/impl/checkout"
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
            snapshot_stage_dir = output.create_stage_dir("snapshot.node")
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
                                Path(tmp_dir)
                                / "cache/snapshots/repo/run/review/checkout"
                            ).as_posix(),
                            "effective_cwd": (
                                Path(tmp_dir)
                                / "cache/snapshots/repo/run/review/checkout"
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
                    event_type="invocation_finished",
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
                    event_type="workspace_context_recorded",
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
            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertIsNotNone(summary.workspace)
            assert summary.workspace is not None
            self.assertIsNotNone(summary.workspace.plan)
            assert summary.workspace.plan is not None
            self.assertEqual(summary.workspace.plan.planned_workspace_node_count, 1)
            self.assertEqual(len(summary.workspace.invocations), 2)
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
            self.assertEqual(worktree_invocation.setup.status, "succeeded")
            self.assertEqual(worktree_invocation.setup.duration_seconds, 1.25)
            self.assertEqual(worktree_invocation.reuse.strategy, "incremental_reset")
            self.assertTrue(worktree_invocation.reuse.reused)
            self.assertEqual(
                worktree_invocation.reuse.reset_verification,
                "verified",
            )
            self.assertEqual(worktree_invocation.execution.checkout_size_bytes, 2048)
            self.assertEqual(worktree_invocation.branch_export.operation, "skipped")
            self.assertEqual(worktree_invocation.checkpoint_count, 1)
            self.assertTrue(snapshot_invocation.snapshot_drift_discarded)
            self.assertEqual(snapshot_invocation.snapshot_changed_paths_reported, 2)
            summary_text = output.get_run_summary_path().read_text(encoding="utf-8")
            self.assertIn("## Workspace Observability", summary_text)
            self.assertIn(f"contract=blob_exact:{SCHEMA_VERSION}", summary_text)
            self.assertIn("rendered workspace files=2", summary_text)
            self.assertIn("launch=runtime_command_runner", summary_text)
            self.assertIn("cleanup_on_success=yes", summary_text)
            self.assertIn("rendered_files=1", summary_text)
            self.assertIn("result=" + "d" * 40, summary_text)
            self.assertIn("bundle=node.a/workspace-bundles/alpha.bundle", summary_text)
            self.assertIn("checkpoints=1", summary_text)
            self.assertIn("setup=node_dependencies, status=succeeded", summary_text)
            self.assertIn("duration=1.250s", summary_text)
            self.assertIn("reuse=incremental_reset, reused=yes", summary_text)
            self.assertIn("reset=verified", summary_text)
            self.assertIn("execution=cache_root=", summary_text)
            self.assertIn("checkout_bytes=2048", summary_text)
            self.assertIn("branch_export=status=skipped", summary_text)
            self.assertIn("operation=skipped", summary_text)
            self.assertIn("snapshot_drift=discarded=yes, changes=2", summary_text)
            terminal_summary = render_run_summary_terminal(summary)
            self.assertIn("Workspace Observability", terminal_summary)
            self.assertIn("launch=runtime_command_runner", terminal_summary)
            self.assertIn("rendered_files=1", terminal_summary)
            self.assertIn("setup=node_dependencies:succeeded", terminal_summary)
            self.assertIn("reuse=incremental_reset,reused=True", terminal_summary)
            self.assertIn("reset=verified", terminal_summary)
            self.assertIn("branch_export=skipped,operation=skipped", terminal_summary)

    def test_workspace_summary_terminal_event_status_wins_over_stale_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stages_dir = Path(tmp_dir)
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

            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0].status, "failed")
            self.assertEqual(merged[0].state_path, "node.a/workspace-state.json")
            self.assertTrue(merged[0].writable)
            self.assertTrue(merged[0].lineage_producer)
            self.assertEqual(
                merged[0].execution.cache_root,
                "/tmp/crewplane-cache",
            )

    def test_workspace_summary_terminal_state_status_wins_over_running_event(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stages_dir = Path(tmp_dir)
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

            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0].status, "cancelled")
            self.assertEqual(merged[0].state_path, "node.a/workspace-state.json")
            self.assertTrue(merged[0].writable)
            self.assertTrue(merged[0].lineage_producer)
            self.assertEqual(
                merged[0].execution.cache_root,
                "/tmp/crewplane-cache",
            )

    def test_workspace_summary_skips_non_utf8_workspace_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stages_dir = Path(tmp_dir)
            stage_dir = stages_dir / "node.a"
            stage_dir.mkdir()
            (stage_dir / "workspace-state.json").write_bytes(b"\xff")

            summaries = workspace_state_summaries(stages_dir)

            self.assertEqual(summaries, ())

    def test_workspace_summary_ignores_non_node_stage_workspace_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stages_dir = Path(tmp_dir)
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

            self.assertEqual(summaries, ())

    def test_workspace_summary_survives_retained_event_detail_cap(self) -> None:
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
                    event_type="invocation_started",
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
                    event_type="workspace_context_recorded",
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
                        event_type="runtime_log",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        level="warning",
                        message=f"summary warning {index}",
                        operation="summary_retention_test",
                    )
                )

            persistent_logger.stop(RunResult(status="succeeded"))

            summary = persistent_logger.last_summary
            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertIsNotNone(summary.workspace)
            assert summary.workspace is not None
            self.assertEqual(
                summary.workspace.invocations[0].workspace_kind,
                "snapshot",
            )
            self.assertEqual(summary.workspace.invocations[0].source.commit, "a" * 40)

    def test_persistent_run_logger_bounds_retained_event_details(self) -> None:
        workflow = single_node_workflow()
        overflow_count = 5
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = OutputManager(workflow.name, base_dir=Path(tmp_dir))
            persistent_logger = PersistentRunLogger(output)

            with ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id=output.run_id,
                observers=[persistent_logger],
                refresh_per_second=0,
            ) as hub:
                for index in range(MAX_RETAINED_SUMMARY_EVENTS + overflow_count):
                    hub.emit(
                        make_execution_event(
                            event_type="runtime_log",
                            workflow_name=workflow.name,
                            run_id=output.run_id,
                            level="warning",
                            message=f"summary warning {index}",
                            operation="summary_retention_test",
                        )
                    )

            self.assertEqual(
                persistent_logger.retained_event_count,
                MAX_RETAINED_SUMMARY_EVENTS,
            )
            self.assertEqual(persistent_logger.dropped_event_count, overflow_count)

            event_log_lines = (
                output.get_run_event_log_path().read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(
                len(event_log_lines),
                MAX_RETAINED_SUMMARY_EVENTS + overflow_count,
            )
            summary_text = output.get_run_summary_path().read_text(encoding="utf-8")
            self.assertIn("were omitted from in-memory summary detail", summary_text)
            self.assertIn(
                f"summary warning {MAX_RETAINED_SUMMARY_EVENTS + overflow_count - 1}",
                summary_text,
            )

    def test_summary_rollups_survive_retained_event_detail_cap(self) -> None:
        workflow = single_node_workflow()
        overflow_count = 5
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
                    event_type="invocation_finished",
                    workflow_name=workflow.name,
                    run_id=output.run_id,
                    node_id="node.a",
                    provider="alpha",
                    role=ProviderRole.EXECUTOR,
                    task_id="alpha_executor_0",
                    attempt_count=1,
                    cli_captured=True,
                    output_extraction_status="success",
                    provider_usage_status="full",
                    provider_tokens={
                        "input": 100,
                        "cached_input": None,
                        "cache_write": None,
                        "output": 20,
                        "reasoning": None,
                        "total": None,
                    },
                    visible_estimate_tokens=50,
                    visible_estimate_method="char-count-lower-bound",
                    visible_estimate_is_lower_bound=True,
                    configured_cost_usd=0.0004,
                    invocation_cost_confidence="full",
                )
            )
            for index in range(MAX_RETAINED_SUMMARY_EVENTS + overflow_count):
                persistent_logger.record_event(
                    make_execution_event(
                        event_type="runtime_log",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        level="warning",
                        message=f"summary warning {index}",
                        operation="summary_retention_test",
                    )
                )

            persistent_logger.stop(RunResult(status="succeeded"))

            self.assertEqual(
                persistent_logger.dropped_event_count,
                overflow_count + 1,
            )
            summary = persistent_logger.last_summary
            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertIsNotNone(summary.spend)
            assert summary.spend is not None
            self.assertEqual(summary.spend.terminal_invocations, 1)
            self.assertEqual(summary.spend.provider_usage_full_invocations, 1)
            self.assertEqual(summary.spend.configured_cost_usd, 0.0004)
            self.assertEqual(len(summary.provider_rollups), 1)
            self.assertEqual(summary.provider_rollups[0].provider, "alpha")

    def test_invocation_usage_details_are_bounded_without_losing_rollups(
        self,
    ) -> None:
        workflow = single_node_workflow()
        overflow_count = 7
        invocation_count = MAX_RETAINED_INVOCATION_USAGE_DETAILS + overflow_count
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
            for index in range(invocation_count):
                persistent_logger.record_event(
                    make_execution_event(
                        event_type="invocation_finished",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="node.a",
                        provider="alpha",
                        role=ProviderRole.EXECUTOR,
                        task_id=f"alpha_task_{index:04d}",
                        attempt_count=1,
                        cli_captured=True,
                        output_extraction_status="success",
                        provider_usage_status="full",
                        provider_tokens={
                            "input": 10,
                            "cached_input": None,
                            "cache_write": None,
                            "output": 2,
                            "reasoning": None,
                            "total": None,
                        },
                        visible_estimate_tokens=12,
                        visible_estimate_method="char-count-lower-bound",
                        visible_estimate_is_lower_bound=True,
                        configured_cost_usd=0.0001,
                        invocation_cost_confidence="full",
                    )
                )

            persistent_logger.stop(RunResult(status="succeeded"))

            summary = persistent_logger.last_summary
            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertEqual(
                len(summary.invocation_usages),
                MAX_RETAINED_INVOCATION_USAGE_DETAILS,
            )
            self.assertEqual(summary.omitted_invocation_usage_count, overflow_count)
            self.assertIsNotNone(summary.spend)
            assert summary.spend is not None
            self.assertEqual(summary.spend.terminal_invocations, invocation_count)
            self.assertEqual(summary.spend.total_attempts, invocation_count)
            self.assertEqual(
                summary.spend.provider_usage_full_invocations, invocation_count
            )
            self.assertEqual(len(summary.provider_rollups), 1)
            self.assertEqual(
                summary.provider_rollups[0].terminal_invocations,
                invocation_count,
            )
            self.assertEqual(
                summary.invocation_usages[0].task_id,
                f"alpha_task_{overflow_count:04d}",
            )

            summary_text = output.get_run_summary_path().read_text(encoding="utf-8")
            self.assertIn(f"- Terminal invocations: {invocation_count}", summary_text)
            self.assertIn(
                "Invocation detail: retained latest "
                f"{MAX_RETAINED_INVOCATION_USAGE_DETAILS} invocation(s); "
                f"{overflow_count} earlier invocation detail(s)",
                summary_text,
            )
            self.assertNotIn("alpha_task_0000", summary_text)
            self.assertIn(f"alpha_task_{invocation_count - 1:04d}", summary_text)
            event_log_lines = (
                output.get_run_event_log_path().read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(len(event_log_lines), invocation_count)

            terminal_summary = render_run_summary_terminal(summary)
            self.assertIn(
                "Invocation detail: retained latest "
                f"{MAX_RETAINED_INVOCATION_USAGE_DETAILS} invocation(s); "
                f"{overflow_count} earlier omitted",
                terminal_summary,
            )

    def test_persistent_run_logger_is_one_shot(self) -> None:
        workflow = single_node_workflow()
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = OutputManager(workflow.name, base_dir=Path(tmp_dir))
            persistent_logger = PersistentRunLogger(output)
            context = RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id=output.run_id,
                refresh_per_second=0,
            )

            persistent_logger.start(context)
            persistent_logger.stop(RunResult(status="succeeded"))

            with self.assertRaisesRegex(RuntimeError, "cannot be restarted"):
                persistent_logger.start(context)

    def test_persistent_run_logger_allows_post_stop_failure_summary_event(self) -> None:
        workflow = single_node_workflow()
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
            persistent_logger.stop(RunResult(status="failed"))

            persistent_logger.record_event(
                make_execution_event(
                    event_type="runtime_log",
                    workflow_name=workflow.name,
                    run_id=output.run_id,
                    level="warning",
                    message="ignored after stop",
                    operation="runtime_warning",
                )
            )
            persistent_logger.record_failure_summary_event(
                workflow_name=workflow.name,
                run_id=output.run_id,
                message="failure after stop",
            )
            summary = persistent_logger.refresh_summary(RunResult(status="failed"))

            self.assertIsNotNone(summary)
            assert summary is not None
            issue_messages = [issue.message for issue in summary.issues]
            self.assertIn("[error] failure after stop", issue_messages)
            self.assertNotIn("ignored after stop", issue_messages)

    def test_persistent_run_summary_labels_audit_round_invocations(self) -> None:
        workflow = WorkflowPlan(
            name="audit.summary",
            nodes=[
                WorkflowNode(
                    id="review.iterate",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="Review")
                    ],
                    providers=[
                        ProviderSpec(provider="codex", role=ProviderRole.EXECUTOR),
                        ProviderSpec(provider="claude", role=ProviderRole.REVIEWER),
                    ],
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output = OutputManager(
                workflow.name,
                base_dir=tmp_path,
                log_cli_output=True,
            )
            stage_dir = output.create_stage_dir("review.iterate")
            persistent_logger = PersistentRunLogger(output)

            with ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id=output.run_id,
                observers=[persistent_logger],
                refresh_per_second=0,
            ) as hub:
                hub.emit(
                    make_execution_event(
                        event_type="workflow_started",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="node_started",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="review.iterate",
                    )
                )
                for audit_round_num in (1, 2):
                    output_file = (
                        stage_dir
                        / f"review-audit-round-{audit_round_num}"
                        / "claude_reviewer_0_round1.md"
                    )
                    log_file = output.get_log_file(
                        stage_name="review.iterate",
                        provider="claude",
                        task_id="claude_reviewer_0",
                        audit_round_num=audit_round_num,
                        round_num=1,
                    )
                    hub.emit(
                        make_execution_event(
                            event_type="invocation_started",
                            workflow_name=workflow.name,
                            run_id=output.run_id,
                            node_id="review.iterate",
                            provider="claude",
                            role=ProviderRole.REVIEWER,
                            model="claude-model",
                            task_id="claude_reviewer_0",
                            audit_round_num=audit_round_num,
                            round_num=1,
                            output_file=str(output_file),
                            log_file=str(log_file),
                        )
                    )
                    hub.emit(
                        make_execution_event(
                            event_type="invocation_finished",
                            workflow_name=workflow.name,
                            run_id=output.run_id,
                            node_id="review.iterate",
                            provider="claude",
                            role=ProviderRole.REVIEWER,
                            model="claude-model",
                            task_id="claude_reviewer_0",
                            audit_round_num=audit_round_num,
                            round_num=1,
                            duration_ms=100,
                            attempt_count=1,
                            cli_captured=True,
                            output_extraction_status="success",
                            provider_usage_status="none",
                            provider_tokens={
                                "input": None,
                                "cached_input": None,
                                "cache_write": None,
                                "output": None,
                                "reasoning": None,
                                "total": None,
                            },
                            visible_estimate_tokens=5,
                            visible_estimate_method="char-count-lower-bound",
                            visible_estimate_is_lower_bound=True,
                            invocation_cost_confidence="none",
                        )
                    )
                hub.emit(
                    make_execution_event(
                        event_type="node_finished",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="review.iterate",
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="runtime_log",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="review.iterate",
                        audit_round_num=2,
                        round_num=1,
                        level="warning",
                        message="review round warning",
                        operation="review_stall_detection",
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="workflow_finished",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                    )
                )

            summary_text = output.get_run_summary_path().read_text(encoding="utf-8")
            self.assertIn(
                "`review.iterate` / `claude_reviewer_0` / `audit1/round1`",
                summary_text,
            )
            self.assertIn(
                "`review.iterate` / `claude_reviewer_0` / `audit2/round1`",
                summary_text,
            )
            self.assertIn("round: audit2/round1", summary_text)
            self.assertEqual(summary_text.count("`claude_reviewer_0`"), 4)


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

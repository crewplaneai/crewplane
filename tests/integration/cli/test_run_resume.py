from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from rich.console import Console

from crewplane.architecture.ports.artifacts import (
    ArtifactStorePort,
    StageTaskSpec,
)
from crewplane.cli.workflow_runner import WorkflowCancelledByUser, execute_workflow_run
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.preflight import PreflightExecutionPlan
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability.types import RunContext, RunResult
from crewplane.runtime.execution.resume import write_successful_node_state
from crewplane.version import SCHEMA_VERSION


def config() -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["mock"], default_model="model")},
        settings=Settings(
            integrations={
                "invoker": {
                    "implementation": "mock",
                    "options": {"observation_delay_seconds": 0, "output_mode": "echo"},
                },
                "ui": {"implementation": "none", "options": {}},
                "artifacts": {
                    "implementation": "filesystem",
                    "options": {"allowed_template_paths": [], "log_cli_output": True},
                },
            }
        ),
    )


def workflow() -> WorkflowPlan:
    return WorkflowPlan(
        name="Resume Workflow",
        nodes=[
            WorkflowNode(
                id="a",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="A")
                ],
            ),
            WorkflowNode(
                id="b",
                mode="sequential",
                needs=["a"],
                providers=[ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="B")
                ],
            ),
        ],
    )


def source(root: Path) -> PreflightWorkflowSource:
    selected_workflow = workflow()
    return PreflightWorkflowSource.from_workflow(
        selected_workflow,
        workflow_content="workflow source",
        composed_workflow={
            "schema_version": selected_workflow.schema_version,
            "name": selected_workflow.name,
            "nodes": [],
        },
        root_workflow_path=root / ".crewplane" / "workflows" / "resume.task.md",
    )


def write_successful_node_output(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    workflow_identity: str,
    node_index: int,
    result_text: str,
) -> None:
    node = plan.nodes[node_index]
    stage_dir = output.create_stage_dir(node.id)
    (stage_dir / "alpha_executor_0_round1.md").write_text(
        result_text,
        encoding="utf-8",
    )
    finalize_result = output.finalize_stage(
        node.id,
        task_specs=(StageTaskSpec("alpha_executor_0", ProviderRole.EXECUTOR),),
    )
    write_successful_node_state(
        node,
        plan,
        output,
        workflow_identity,
        finalize_result,
    )


class CliRunResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_run_resumes_validated_node_boundary_into_fresh_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            console = Console(file=io.StringIO(), force_terminal=False)
            original_cwd = Path.cwd()
            calls: list[tuple[str, ...]] = []

            async def fake_execute_workflow(plan, output, **kwargs):  # type: ignore[no-untyped-def]
                resumed_node_ids = tuple(kwargs.get("resumed_node_ids", ()))
                calls.append(resumed_node_ids)
                if len(calls) == 1:
                    write_successful_node_output(
                        plan,
                        output,
                        kwargs["workflow_identity"],
                        0,
                        "A result",
                    )
                    raise RuntimeError("B failed")
                assert resumed_node_ids == ("a",)
                assert output.get_stage_output_path("a").exists()
                assert (output.stages_dir / "a" / "resume-source.json").exists()
                write_successful_node_output(
                    plan,
                    output,
                    kwargs["workflow_identity"],
                    1,
                    "B result",
                )

            os.chdir(root)
            try:
                with self.assertRaisesRegex(RuntimeError, "B failed"):
                    await execute_workflow_run(
                        config=config(),
                        source=source(root),
                        force=False,
                        no_live=True,
                        console=console,
                        execute_workflow_impl=fake_execute_workflow,
                        project_root=root,
                        state_dir=root / ".crewplane",
                    )
                await execute_workflow_run(
                    config=config(),
                    source=source(root),
                    force=False,
                    no_live=True,
                    console=console,
                    execute_workflow_impl=fake_execute_workflow,
                    project_root=root,
                    state_dir=root / ".crewplane",
                )
            finally:
                os.chdir(original_cwd)

            self.assertEqual(calls, [(), ("a",)])
            run_dirs = sorted((root / ".crewplane" / "execution-stages").iterdir())
            self.assertEqual(len(run_dirs), 2)
            second_run = run_dirs[1]
            self.assertTrue((second_run / "a" / "resume-source.json").exists())
            self.assertFalse((second_run / "a" / "alpha_executor_0_round1.md").exists())
            manifest = json.loads(
                (second_run / "manifests" / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "succeeded")
            self.assertEqual(manifest["resumed_nodes"], ["a"])

    async def test_cancelled_run_resumes_validated_node_boundary_into_fresh_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            console = Console(file=io.StringIO(), force_terminal=False)
            original_cwd = Path.cwd()
            calls: list[tuple[str, ...]] = []

            async def fake_execute_workflow(plan, output, **kwargs):  # type: ignore[no-untyped-def]
                resumed_node_ids = tuple(kwargs.get("resumed_node_ids", ()))
                calls.append(resumed_node_ids)
                if len(calls) == 1:
                    write_successful_node_output(
                        plan,
                        output,
                        kwargs["workflow_identity"],
                        0,
                        "A result",
                    )
                    raise asyncio.CancelledError()
                assert resumed_node_ids == ("a",)
                assert output.get_stage_output_path("a").exists()
                assert (output.stages_dir / "a" / "resume-source.json").exists()
                write_successful_node_output(
                    plan,
                    output,
                    kwargs["workflow_identity"],
                    1,
                    "B result",
                )

            os.chdir(root)
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await execute_workflow_run(
                        config=config(),
                        source=source(root),
                        force=False,
                        no_live=True,
                        console=console,
                        execute_workflow_impl=fake_execute_workflow,
                        project_root=root,
                        state_dir=root / ".crewplane",
                    )
                await execute_workflow_run(
                    config=config(),
                    source=source(root),
                    force=False,
                    no_live=True,
                    console=console,
                    execute_workflow_impl=fake_execute_workflow,
                    project_root=root,
                    state_dir=root / ".crewplane",
                )
            finally:
                os.chdir(original_cwd)

            self.assertEqual(calls, [(), ("a",)])
            run_dirs = sorted((root / ".crewplane" / "execution-stages").iterdir())
            self.assertEqual(len(run_dirs), 2)
            first_manifest = json.loads(
                (run_dirs[0] / "manifests" / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(first_manifest["status"], "cancelled")
            self.assertEqual(
                first_manifest["cancel_reason"],
                "external_cancellation",
            )
            self.assertIsNotNone(first_manifest["completed_at"])
            first_summary = (run_dirs[0] / "logs" / "summary.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("- Status: cancelled", first_summary)
            second_run = run_dirs[1]
            self.assertTrue((second_run / "a" / "resume-source.json").exists())
            second_manifest = json.loads(
                (second_run / "manifests" / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(second_manifest["status"], "succeeded")
            self.assertEqual(second_manifest["resumed_nodes"], ["a"])

    async def test_live_dashboard_cancelled_run_resumes_validated_node_boundary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            console = Console(file=io.StringIO(), force_terminal=False)
            original_cwd = Path.cwd()
            calls: list[tuple[str, ...]] = []
            hub_instances = []

            class StopRequestedHub:
                def __init__(
                    self,
                    workflow_topology,
                    run_id: str,
                    observers,
                    refresh_per_second: int = 4,
                    warning_sink=None,
                ) -> None:
                    self._context = RunContext(
                        workflow_topology=workflow_topology,
                        run_id=run_id,
                        refresh_per_second=refresh_per_second,
                    )
                    self._observers = list(observers)
                    self._terminal_result: RunResult | None = None
                    self.stop_requested = False
                    self.active_observer_count = 0
                    self.warning_sink = warning_sink
                    hub_instances.append(self)

                def __enter__(self):
                    for observer in self._observers:
                        observer.start(self._context)
                    self.active_observer_count = len(self._observers)
                    return self

                def __exit__(self, exc_type, _exc, _traceback) -> None:
                    result = self._terminal_result or RunResult(
                        status="failed" if exc_type is not None else "succeeded"
                    )
                    for observer in reversed(self._observers):
                        observer.stop(result)

                def emit(self, event) -> None:
                    del event
                    return None

                def set_terminal_result(self, result: RunResult) -> None:
                    self._terminal_result = result

                def request_stop(self) -> None:
                    self.stop_requested = True

            async def fake_execute_workflow(plan, output, **kwargs):  # type: ignore[no-untyped-def]
                resumed_node_ids = tuple(kwargs.get("resumed_node_ids", ()))
                calls.append(resumed_node_ids)
                if len(calls) == 1:
                    write_successful_node_output(
                        plan,
                        output,
                        kwargs["workflow_identity"],
                        0,
                        "A result",
                    )
                    hub_instances[-1].request_stop()
                    await asyncio.sleep(5)
                    raise AssertionError("Stop request did not cancel workflow.")
                assert resumed_node_ids == ("a",)
                assert output.get_stage_output_path("a").exists()
                assert (output.stages_dir / "a" / "resume-source.json").exists()
                write_successful_node_output(
                    plan,
                    output,
                    kwargs["workflow_identity"],
                    1,
                    "B result",
                )

            os.chdir(root)
            try:
                with self.assertRaises(WorkflowCancelledByUser):
                    await execute_workflow_run(
                        config=config(),
                        source=source(root),
                        force=False,
                        no_live=True,
                        console=console,
                        execute_workflow_impl=fake_execute_workflow,
                        observability_hub_cls=StopRequestedHub,
                        project_root=root,
                        state_dir=root / ".crewplane",
                    )
                await execute_workflow_run(
                    config=config(),
                    source=source(root),
                    force=False,
                    no_live=True,
                    console=console,
                    execute_workflow_impl=fake_execute_workflow,
                    project_root=root,
                    state_dir=root / ".crewplane",
                )
            finally:
                os.chdir(original_cwd)

            self.assertEqual(calls, [(), ("a",)])
            run_dirs = sorted((root / ".crewplane" / "execution-stages").iterdir())
            self.assertEqual(len(run_dirs), 2)
            first_manifest = json.loads(
                (run_dirs[0] / "manifests" / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(first_manifest["status"], "cancelled")
            self.assertEqual(first_manifest["cancel_reason"], "ui_stop_requested")
            second_run = run_dirs[1]
            self.assertTrue((second_run / "a" / "resume-source.json").exists())
            second_manifest = json.loads(
                (second_run / "manifests" / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(second_manifest["status"], "succeeded")
            self.assertEqual(second_manifest["resumed_nodes"], ["a"])

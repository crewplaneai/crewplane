from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from rich.console import Console

from orchestrator_cli.architecture.ports.artifacts import (
    ArtifactStorePort,
    StageTaskSpec,
)
from orchestrator_cli.cli.workflow_runner import execute_workflow_run
from orchestrator_cli.core.config import AgentConfig, Config, Settings
from orchestrator_cli.core.preflight import PreflightExecutionPlan
from orchestrator_cli.core.preflight.source import PreflightWorkflowSource
from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.runtime.execution.resume import write_successful_node_state
from orchestrator_cli.version import SCHEMA_VERSION


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
                providers=[ProviderSpec(provider="alpha", role="executor")],
                prompt_segments=[PromptSegment(role="shared", content="A")],
            ),
            WorkflowNode(
                id="b",
                mode="sequential",
                needs=["a"],
                providers=[ProviderSpec(provider="alpha", role="executor")],
                prompt_segments=[PromptSegment(role="shared", content="B")],
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
        root_workflow_path=root / ".orchestrator" / "workflows" / "resume.task.md",
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
        task_specs=(StageTaskSpec("alpha_executor_0", "executor"),),
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
                        orchestrator_dir=root / ".orchestrator",
                    )
                await execute_workflow_run(
                    config=config(),
                    source=source(root),
                    force=False,
                    no_live=True,
                    console=console,
                    execute_workflow_impl=fake_execute_workflow,
                    project_root=root,
                    orchestrator_dir=root / ".orchestrator",
                )
            finally:
                os.chdir(original_cwd)

            self.assertEqual(calls, [(), ("a",)])
            run_dirs = sorted((root / ".orchestrator" / "execution-stages").iterdir())
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
                        orchestrator_dir=root / ".orchestrator",
                    )
                await execute_workflow_run(
                    config=config(),
                    source=source(root),
                    force=False,
                    no_live=True,
                    console=console,
                    execute_workflow_impl=fake_execute_workflow,
                    project_root=root,
                    orchestrator_dir=root / ".orchestrator",
                )
            finally:
                os.chdir(original_cwd)

            self.assertEqual(calls, [(), ("a",)])
            run_dirs = sorted((root / ".orchestrator" / "execution-stages").iterdir())
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

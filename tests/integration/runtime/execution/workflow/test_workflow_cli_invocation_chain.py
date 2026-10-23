import unittest
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch

import pytest

from crewplane.adapters.invokers.cli_invoker import (
    build_cli_invocation_plan,
    build_cli_log_presentation,
)
from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
    CommandRunner,
    InvocationContext,
)
from crewplane.artifacts import OutputManager
from crewplane.core.config import AgentConfig, Config
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.runtime.agent.invoker import PlannedAgentInvoker
from crewplane.version import SCHEMA_VERSION
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    execute_workflow,
)

pytestmark = pytest.mark.usefixtures("posix_cli_plans")


def _invocation_recorder(invocation_contexts: list[InvocationContext]) -> CommandRunner:
    async def record_invocation(
        cmd: list[str],
        stdin_data: bytes | None,
        log_file: Path | None,
        append_log: bool,
        log_header: bytes | None,
        cwd: Path,
        invocation_context: InvocationContext | None,
        idle_timeout_seconds: float | None,
        child_environment: ChildProcessEnvironment | None = None,
    ) -> CommandResult:
        del (
            cmd,
            stdin_data,
            log_file,
            append_log,
            log_header,
            cwd,
            idle_timeout_seconds,
            child_environment,
        )
        if invocation_context is None:
            raise AssertionError("expected invocation_context for workflow execution")
        invocation_contexts.append(invocation_context)
        return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

    return record_invocation


class WorkflowCliInvocationChainTests(unittest.IsolatedAsyncioTestCase):
    @pytest.fixture(autouse=True)
    def temporary_directory_root(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    async def test_single_provider_sequential_node_invokes_cli_once_without_retries(
        self,
    ) -> None:
        tmp_dir = mkdtemp(dir=self.tmp_path)
        tmp_path = Path(tmp_dir)
        config = Config(
            version=SCHEMA_VERSION,
            agents={
                "alpha": AgentConfig(cli_cmd=["./mock"], default_model="alpha"),
            },
        )
        workflow = WorkflowPlan(
            name="single-sequential",
            nodes=[
                WorkflowNode(
                    id="node.single",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="run once")
                    ],
                    providers=[
                        ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                    ],
                )
            ],
        )
        output = OutputManager(workflow.name, base_dir=tmp_path)
        invocation_contexts: list[InvocationContext] = []

        with patch(
            "crewplane.runtime.agent.invocation.command.run_command_once",
            new=_invocation_recorder(invocation_contexts),
        ):
            await execute_workflow(
                config,
                workflow,
                output,
                invoker=PlannedAgentInvoker(
                    plan_builder=build_cli_invocation_plan,
                    log_presentation_builder=build_cli_log_presentation,
                ),
                suppress_progress_output=True,
            )

        self.assertEqual(len(invocation_contexts), 1)
        context = invocation_contexts[0]
        self.assertEqual(context.node_id, "node.single")
        self.assertEqual(context.task_id, "alpha_executor_0")
        self.assertEqual(context.round_num, 1)

    async def test_single_provider_parallel_node_invokes_cli_once_without_retries(
        self,
    ) -> None:
        tmp_dir = mkdtemp(dir=self.tmp_path)
        tmp_path = Path(tmp_dir)
        config = Config(
            version=SCHEMA_VERSION,
            agents={
                "alpha": AgentConfig(cli_cmd=["./mock"], default_model="alpha"),
            },
        )
        workflow = WorkflowPlan(
            name="single-parallel",
            nodes=[
                WorkflowNode(
                    id="node.single",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="run once")
                    ],
                    providers=[ProviderSpec(provider="alpha")],
                )
            ],
        )
        output = OutputManager(workflow.name, base_dir=tmp_path)
        invocation_contexts: list[InvocationContext] = []

        with patch(
            "crewplane.runtime.agent.invocation.command.run_command_once",
            new=_invocation_recorder(invocation_contexts),
        ):
            await execute_workflow(
                config,
                workflow,
                output,
                invoker=PlannedAgentInvoker(
                    plan_builder=build_cli_invocation_plan,
                    log_presentation_builder=build_cli_log_presentation,
                ),
                suppress_progress_output=True,
            )

        self.assertEqual(len(invocation_contexts), 1)
        context = invocation_contexts[0]
        self.assertEqual(context.node_id, "node.single")
        self.assertEqual(context.task_id, "alpha_executor_0")
        self.assertEqual(context.round_num, 1)

    async def test_single_provider_per_node_workflow_invokes_once_per_node(
        self,
    ) -> None:
        tmp_dir = mkdtemp(dir=self.tmp_path)
        tmp_path = Path(tmp_dir)
        config = Config(
            version=SCHEMA_VERSION,
            agents={
                "alpha": AgentConfig(cli_cmd=["./mock"], default_model="alpha"),
            },
        )
        workflow = WorkflowPlan(
            name="once-per-node",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="a")
                    ],
                    providers=[
                        ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                    ],
                ),
                WorkflowNode(
                    id="node.b",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="b")
                    ],
                    providers=[ProviderSpec(provider="alpha")],
                ),
                WorkflowNode(
                    id="node.c",
                    mode="sequential",
                    needs=["node.a", "node.b"],
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="c")
                    ],
                    providers=[
                        ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                    ],
                ),
            ],
        )
        output = OutputManager(workflow.name, base_dir=tmp_path)
        invocation_contexts: list[InvocationContext] = []

        with patch(
            "crewplane.runtime.agent.invocation.command.run_command_once",
            new=_invocation_recorder(invocation_contexts),
        ):
            await execute_workflow(
                config,
                workflow,
                output,
                invoker=PlannedAgentInvoker(
                    plan_builder=build_cli_invocation_plan,
                    log_presentation_builder=build_cli_log_presentation,
                ),
                suppress_progress_output=True,
            )

        per_node_counts: dict[str, int] = {}
        for context in invocation_contexts:
            per_node_counts[context.node_id] = (
                per_node_counts.get(context.node_id, 0) + 1
            )
        self.assertEqual(
            per_node_counts,
            {
                "node.a": 1,
                "node.b": 1,
                "node.c": 1,
            },
        )
        self.assertEqual(len(invocation_contexts), 3)

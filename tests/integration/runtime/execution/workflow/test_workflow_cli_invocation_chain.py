import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crewplane.adapters.invokers.cli_invoker import (
    build_cli_invocation_plan,
    build_cli_log_presentation,
)
from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
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


class WorkflowCliInvocationChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_provider_sequential_node_invokes_cli_once_without_retries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
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
                            PromptSegment(
                                role=PromptSegmentRole.SHARED, content="run once"
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                        ],
                    )
                ],
            )
            output = OutputManager(workflow.name, base_dir=tmp_path)
            invocation_contexts = []

            async def fake_run_command_once(
                cmd: list[str],  # noqa: ARG001 - Required by test double or callback signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by test double or callback signature.
                log_file: Path | None,  # noqa: ARG001 - Required by test double or callback signature.
                append_log: bool,  # noqa: ARG001 - Required by test double or callback signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by test double or callback signature.
                cwd: Path,  # noqa: ARG001 - Required by test double or callback signature.
                invocation_context,  # type: ignore[no-untyped-def]
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by test double or callback signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by test double or callback signature.
            ) -> CommandResult:
                if invocation_context is None:
                    raise AssertionError(
                        "expected invocation_context for workflow execution"
                    )
                invocation_contexts.append(invocation_context)
                return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

            with patch(
                "crewplane.runtime.agent.invocation.command.run_command_once",
                side_effect=fake_run_command_once,
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
        with tempfile.TemporaryDirectory() as tmp_dir:
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
                            PromptSegment(
                                role=PromptSegmentRole.SHARED, content="run once"
                            )
                        ],
                        providers=[ProviderSpec(provider="alpha")],
                    )
                ],
            )
            output = OutputManager(workflow.name, base_dir=tmp_path)
            invocation_contexts = []

            async def fake_run_command_once(
                cmd: list[str],  # noqa: ARG001 - Required by test double or callback signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by test double or callback signature.
                log_file: Path | None,  # noqa: ARG001 - Required by test double or callback signature.
                append_log: bool,  # noqa: ARG001 - Required by test double or callback signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by test double or callback signature.
                cwd: Path,  # noqa: ARG001 - Required by test double or callback signature.
                invocation_context,  # type: ignore[no-untyped-def]
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by test double or callback signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by test double or callback signature.
            ) -> CommandResult:
                if invocation_context is None:
                    raise AssertionError(
                        "expected invocation_context for workflow execution"
                    )
                invocation_contexts.append(invocation_context)
                return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

            with patch(
                "crewplane.runtime.agent.invocation.command.run_command_once",
                side_effect=fake_run_command_once,
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
        with tempfile.TemporaryDirectory() as tmp_dir:
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
            invocation_contexts = []

            async def fake_run_command_once(
                cmd: list[str],  # noqa: ARG001 - Required by test double or callback signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by test double or callback signature.
                log_file: Path | None,  # noqa: ARG001 - Required by test double or callback signature.
                append_log: bool,  # noqa: ARG001 - Required by test double or callback signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by test double or callback signature.
                cwd: Path,  # noqa: ARG001 - Required by test double or callback signature.
                invocation_context,  # type: ignore[no-untyped-def]
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by test double or callback signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by test double or callback signature.
            ) -> CommandResult:
                if invocation_context is None:
                    raise AssertionError(
                        "expected invocation_context for workflow execution"
                    )
                invocation_contexts.append(invocation_context)
                return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

            with patch(
                "crewplane.runtime.agent.invocation.command.run_command_once",
                side_effect=fake_run_command_once,
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

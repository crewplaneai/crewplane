import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.adapters.invokers.cli_invoker import (
    build_cli_invocation_plan,
    build_cli_log_presentation,
)
from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
    EventType,
    InvocationContext,
    JsonObject,
    LogPresentationDescriptor,
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
from crewplane.observability.events import ExecutionEvent
from crewplane.runtime.agent.invoker import PlannedAgentInvoker
from crewplane.runtime.execution import WorkflowExecutionError
from crewplane.runtime.workspace.setup import WorkspaceSetupError
from crewplane.version import SCHEMA_VERSION
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    SelectiveFailInvoker,
    execute_workflow,
)


class WorkflowInputBudgetFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_node_blocks_dependents_but_independent_nodes_continue(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "ok": AgentConfig(cli_cmd=["mock"], default_model="ok"),
                    "fail": AgentConfig(cli_cmd=["mock"], default_model="fail"),
                },
            )
            workflow = WorkflowPlan(
                name="dag.failure",
                nodes=[
                    WorkflowNode(
                        id="node.root.fail",
                        mode="parallel",
                        prompt_segments=[
                            PromptSegment(role=PromptSegmentRole.SHARED, content="fail")
                        ],
                        providers=[ProviderSpec(provider="fail")],
                        failure_threshold=0,
                    ),
                    WorkflowNode(
                        id="node.root.ok",
                        mode="sequential",
                        prompt_segments=[
                            PromptSegment(role=PromptSegmentRole.SHARED, content="ok")
                        ],
                        providers=[
                            ProviderSpec(provider="ok", role=ProviderRole.EXECUTOR)
                        ],
                    ),
                    WorkflowNode(
                        id="node.dep",
                        mode="sequential",
                        needs=["node.root.fail"],
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED, content="dependent"
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="ok", role=ProviderRole.EXECUTOR)
                        ],
                    ),
                ],
            )
            invoker = SelectiveFailInvoker(failing_models={"fail"})
            output = OutputManager(workflow.name, base_dir=tmp_path)

            with pytest.raises(WorkflowExecutionError) as raised:
                await execute_workflow(config, workflow, output, invoker=invoker)

            failure_message = str(raised.value)
            assert "Workflow 'dag.failure' failed:" in failure_message
            assert "- failed: node.root.fail" in failure_message
            assert "exceeded failure threshold" in failure_message
            assert (
                "- blocked: node.dep (unsatisfied dependencies: node.root.fail)"
                in failure_message
            )
            executed_models = sorted(call["model"] for call in invoker.calls)
            assert "ok" in executed_models
            assert "fail" in executed_models

    async def test_unexpected_node_task_exception_propagates_without_aggregation(
        self,
    ) -> None:
        class DefectiveOutputManager(OutputManager):
            def finalize_node(self, *args: object, **kwargs: object) -> object:
                del args, kwargs
                raise RuntimeError("simulated finalize defect")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_file = tmp_path / ".crewplane" / "inputs" / "source.md"
            input_file.parent.mkdir(parents=True, exist_ok=True)
            input_file.write_text("source", encoding="utf-8")
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="alpha"),
                },
            )
            workflow = WorkflowPlan(
                name="dag.unexpected.node.defect",
                nodes=[
                    WorkflowNode(
                        id="input",
                        mode="input",
                        source="{{file:.crewplane/inputs/source.md}}",
                    )
                ],
            )
            invoker = MockAgentInvoker()
            output = DefectiveOutputManager(workflow.name, base_dir=tmp_path)
            events: list[ExecutionEvent] = []

            with pytest.raises(
                RuntimeError, match="simulated finalize defect"
            ) as raised:
                await execute_workflow(
                    config,
                    workflow,
                    output,
                    invoker=invoker,
                    event_sink=events.append,
                )

            assert type(raised.value) is RuntimeError
            node_failed_events = [
                event
                for event in events
                if event.event_type == EventType.NODE_FAILED
                and event.context.node_id == "input"
            ]
            assert len(node_failed_events) == 1

    async def test_concurrent_unexpected_node_failures_are_drained(
        self,
    ) -> None:
        class DefectiveOutputManager(OutputManager):
            def finalize_node(self, *args: object, **kwargs: object) -> object:
                del args, kwargs
                raise RuntimeError("simulated finalize defect")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            for file_name in ("first.md", "second.md"):
                input_file = tmp_path / ".crewplane" / "inputs" / file_name
                input_file.parent.mkdir(parents=True, exist_ok=True)
                input_file.write_text("source", encoding="utf-8")
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="alpha"),
                },
            )
            workflow = WorkflowPlan(
                name="dag.concurrent.unexpected.node.defects",
                nodes=[
                    WorkflowNode(
                        id="first",
                        mode="input",
                        source="{{file:.crewplane/inputs/first.md}}",
                    ),
                    WorkflowNode(
                        id="second",
                        mode="input",
                        source="{{file:.crewplane/inputs/second.md}}",
                    ),
                ],
            )
            invoker = MockAgentInvoker()
            output = DefectiveOutputManager(workflow.name, base_dir=tmp_path)
            events: list[ExecutionEvent] = []
            loop = asyncio.get_running_loop()
            original_handler = loop.get_exception_handler()
            loop_exception_contexts: list[dict[str, object]] = []

            def capture_loop_exception(
                loop: asyncio.AbstractEventLoop,
                context: dict[str, object],
            ) -> None:
                del loop
                loop_exception_contexts.append(context)

            loop.set_exception_handler(capture_loop_exception)
            try:
                with pytest.raises(RuntimeError, match="simulated finalize defect"):
                    await execute_workflow(
                        config,
                        workflow,
                        output,
                        invoker=invoker,
                        event_sink=events.append,
                    )
                await asyncio.sleep(0)
            finally:
                loop.set_exception_handler(original_handler)

            failed_node_ids = [
                event.context.node_id
                for event in events
                if event.event_type == EventType.NODE_FAILED
            ]
            assert failed_node_ids == ["first", "second"]
            never_retrieved_contexts = [
                context
                for context in loop_exception_contexts
                if "exception was never retrieved" in str(context.get("message", ""))
            ]
            assert never_retrieved_contexts == []

    async def test_unexpected_parallel_invoker_exception_propagates_without_aggregation(
        self,
    ) -> None:
        class DefectiveInvoker:
            def log_presentation_for(
                self,
                config: AgentConfig,  # noqa: ARG002 - Required by invoker protocol.
            ) -> LogPresentationDescriptor | None:
                return None

            async def invoke(
                self,
                config: AgentConfig,  # noqa: ARG002 - Required by invoker protocol.
                model: str | None,
                prompt: str,  # noqa: ARG002 - Required by invoker protocol.
                output_file: Path,
                cwd: Path,  # noqa: ARG002 - Required by invoker protocol.
                log_file: Path | None = None,  # noqa: ARG002 - Required by invoker protocol.
                invocation_context: InvocationContext | None = None,  # noqa: ARG002 - Required by invoker protocol.
            ) -> None:
                if model == "defective":
                    raise TypeError("simulated invoker defect")
                output_file.write_text("success", encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "ok": AgentConfig(cli_cmd=["mock"], default_model="ok"),
                    "defective": AgentConfig(
                        cli_cmd=["mock"], default_model="defective"
                    ),
                },
            )
            workflow = WorkflowPlan(
                name="dag.unexpected.invoker.defect",
                nodes=[
                    WorkflowNode(
                        id="parallel",
                        mode="parallel",
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED,
                                content="run defective provider",
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="ok"),
                            ProviderSpec(provider="defective"),
                        ],
                        failure_threshold=1,
                    )
                ],
            )
            output = OutputManager(workflow.name, base_dir=tmp_path)

            with pytest.raises(TypeError, match="simulated invoker defect") as raised:
                await execute_workflow(
                    config,
                    workflow,
                    output,
                    invoker=DefectiveInvoker(),
                )

            assert type(raised.value) is TypeError

    async def test_workspace_setup_failure_is_aggregated_as_workflow_failure(
        self,
    ) -> None:
        class SetupFailureInvoker:
            def log_presentation_for(
                self,
                config: AgentConfig,  # noqa: ARG002 - Required by invoker protocol.
            ) -> LogPresentationDescriptor | None:
                return None

            async def invoke(
                self,
                config: AgentConfig,  # noqa: ARG002 - Required by invoker protocol.
                model: str | None,  # noqa: ARG002 - Required by invoker protocol.
                prompt: str,  # noqa: ARG002 - Required by invoker protocol.
                output_file: Path,  # noqa: ARG002 - Required by invoker protocol.
                cwd: Path,  # noqa: ARG002 - Required by invoker protocol.
                log_file: Path | None = None,  # noqa: ARG002 - Required by invoker protocol.
                invocation_context: InvocationContext | None = None,  # noqa: ARG002 - Required by invoker protocol.
            ) -> None:
                summary: JsonObject = {"status": "failed"}
                raise WorkspaceSetupError("workspace setup command failed", summary)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "setup": AgentConfig(cli_cmd=["mock"], default_model="setup"),
                    "dependent": AgentConfig(
                        cli_cmd=["mock"], default_model="dependent"
                    ),
                },
            )
            workflow = WorkflowPlan(
                name="dag.workspace.setup.failure",
                nodes=[
                    WorkflowNode(
                        id="node.setup",
                        mode="sequential",
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED,
                                content="run setup",
                            )
                        ],
                        providers=[ProviderSpec(provider="setup")],
                    ),
                    WorkflowNode(
                        id="node.dep",
                        mode="parallel",
                        needs=["node.setup"],
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED,
                                content="blocked by setup",
                            )
                        ],
                        providers=[ProviderSpec(provider="dependent")],
                    ),
                ],
            )
            output = OutputManager(workflow.name, base_dir=tmp_path)

            with pytest.raises(WorkflowExecutionError) as raised:
                await execute_workflow(
                    config,
                    workflow,
                    output,
                    invoker=SetupFailureInvoker(),
                )

            failure_message = str(raised.value)
            assert "- failed: node.setup" in failure_message
            assert "workspace setup command failed" in failure_message
            assert (
                "- blocked: node.dep (unsatisfied dependencies: node.setup)"
                in failure_message
            )

    async def test_blocked_nodes_emit_node_blocked_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "ok": AgentConfig(cli_cmd=["mock"], default_model="ok"),
                    "fail": AgentConfig(cli_cmd=["mock"], default_model="fail"),
                },
            )
            workflow = WorkflowPlan(
                name="dag.blocked.events",
                nodes=[
                    WorkflowNode(
                        id="node.root.fail",
                        mode="sequential",
                        prompt_segments=[
                            PromptSegment(role=PromptSegmentRole.SHARED, content="fail")
                        ],
                        providers=[
                            ProviderSpec(provider="fail", role=ProviderRole.EXECUTOR)
                        ],
                    ),
                    WorkflowNode(
                        id="node.dep",
                        mode="sequential",
                        needs=["node.root.fail"],
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED, content="dependent"
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="ok", role=ProviderRole.EXECUTOR)
                        ],
                    ),
                ],
            )
            invoker = SelectiveFailInvoker(failing_models={"fail"})
            output = OutputManager(workflow.name, base_dir=tmp_path)
            events = []

            with pytest.raises(WorkflowExecutionError, match="blocked: node.dep"):
                await execute_workflow(
                    config,
                    workflow,
                    output,
                    invoker=invoker,
                    event_sink=events.append,
                )

            blocked_events = [
                event for event in events if event.event_type == EventType.NODE_BLOCKED
            ]
            assert len(blocked_events) == 1
            assert blocked_events[0].context.node_id == "node.dep"
            blocked_runtime_logs = [
                event
                for event in events
                if event.event_type == EventType.RUNTIME_LOG
                and event.payload.operation == "blocked_dependencies"
            ]
            assert len(blocked_runtime_logs) == 1
            assert blocked_runtime_logs[0].context.node_id == "node.dep"
            assert (
                "unsatisfied dependencies: node.root.fail"
                in blocked_runtime_logs[0].payload.message
            )

    async def test_nonzero_exit_still_emits_invocation_failed_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(
                        cli_cmd=["./alpha-cli"],
                        default_model="alpha",
                    ),
                },
            )
            workflow = WorkflowPlan(
                name="nonzero.exit",
                nodes=[
                    WorkflowNode(
                        id="node.fail",
                        mode="sequential",
                        prompt_segments=[
                            PromptSegment(role=PromptSegmentRole.SHARED, content="fail")
                        ],
                        providers=[
                            ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                        ],
                    )
                ],
            )
            output = OutputManager(
                workflow.name, base_dir=tmp_path, log_cli_output=True
            )
            events: list[ExecutionEvent] = []

            async def failing_command_runner(
                cmd: list[str],  # noqa: ARG001 - Required by test double or callback signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by test double or callback signature.
                log_file: Path | None,
                append_log: bool,  # noqa: ARG001 - Required by test double or callback signature.
                log_header: bytes | None,
                cwd: Path,  # noqa: ARG001 - Required by test double or callback signature.
                invocation_context,  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by test double or callback signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by test double or callback signature.
            ) -> CommandResult:
                assert log_file is not None
                log_file.parent.mkdir(parents=True, exist_ok=True)
                if log_header is not None:
                    log_file.write_bytes(log_header)
                return CommandResult(returncode=2, stdout_text="", stderr_text="boom")

            with (
                patch(
                    "crewplane.runtime.agent.invocation.command.run_command_once",
                    failing_command_runner,
                ),
                pytest.raises(RuntimeError, match="Exit code 2: boom"),
            ):
                await execute_workflow(
                    config,
                    workflow,
                    output,
                    invoker=PlannedAgentInvoker(
                        plan_builder=build_cli_invocation_plan,
                        log_presentation_builder=build_cli_log_presentation,
                    ),
                    event_sink=events.append,
                )

            invocation_failed_events = [
                event
                for event in events
                if event.event_type == EventType.INVOCATION_FAILED
            ]
            assert len(invocation_failed_events) == 1
            assert invocation_failed_events[0].context.node_id == "node.fail"
            assert invocation_failed_events[0].payload.error == "Exit code 2: boom"
            assert invocation_failed_events[0].context.log_file is not None

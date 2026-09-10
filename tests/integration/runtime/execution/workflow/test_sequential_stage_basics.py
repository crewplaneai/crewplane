import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.artifacts import OutputManager, safe_artifact_name
from crewplane.core.config import AgentConfig, Config
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
)
from crewplane.runtime.execution.errors import NodeExecutionError
from crewplane.runtime.execution.fragment_assembler import ResolvedPrompt
from crewplane.version import SCHEMA_VERSION
from tests.helpers.artifacts import node_artifact_request
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    execute_sequential_stage,
)


class ExecutorSequentialStageBasicsTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_provider_sequential_respects_depth_and_sanitizes_filename(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "solo/provider": AgentConfig(
                        cli_cmd=["mock"],
                        default_model="test-model",
                    )
                },
            )

            node = WorkflowNode(
                id="single.provider.node",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                ],
                depth=3,
                providers=[
                    ProviderSpec(provider="solo/provider", role=ProviderRole.EXECUTOR)
                ],
            )
            invoker = MockAgentInvoker(outputs=["one", "two", "three"])
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(config, node, output, invoker=invoker)

            node_dir = output.get_node_dir(node_artifact_request(node.id))
            if node_dir is None:
                self.fail("Expected node directory to be created")

            safe_provider = safe_artifact_name("solo/provider")
            produced_files = sorted(path.name for path in node_dir.glob("*.md"))
            assert produced_files == [
                f"{safe_provider}_executor_0_round1.md",
                f"{safe_provider}_executor_0_round2.md",
                f"{safe_provider}_executor_0_round3.md",
            ]

    async def test_single_provider_sequential_rerenders_prompt_per_round(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={"exec": AgentConfig(cli_cmd=["mock"])},
            )
            node = WorkflowNode(
                id="single.provider.rerender",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                ],
                depth=3,
                providers=[ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR)],
            )
            invoker = MockAgentInvoker(outputs=["one", "two", "three"])
            output = OutputManager("workflow", base_dir=tmp_path)
            candidate_source_flags: list[bool] = []

            def resolve_prompt(*args, **kwargs) -> ResolvedPrompt:  # type: ignore[no-untyped-def]
                del args
                candidate_source = kwargs["workspace_candidate_source"]
                candidate_source_flags.append(candidate_source)
                return ResolvedPrompt(f"prompt candidate={candidate_source}")

            with patch(
                "crewplane.runtime.execution.sequential.resolve_prompt_with_output_budget_details",
                side_effect=resolve_prompt,
            ):
                await execute_sequential_stage(config, node, output, invoker=invoker)

            assert candidate_source_flags == [False, True, True]
            assert [call["prompt"] for call in invoker.calls] == [
                "prompt candidate=False",
                "prompt candidate=True",
                "prompt candidate=True",
            ]

    async def test_single_provider_sequential_fails_when_resolved_executor_prompt_is_empty(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={"exec": AgentConfig(cli_cmd=["mock"], default_model="m1")},
            )
            node = WorkflowNode(
                id="single.provider.empty.prompt",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(
                        role="shared", content="{{env:EMPTY_EXECUTOR_PROMPT}}"
                    )
                ],
                providers=[ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR)],
            )
            invoker = MockAgentInvoker(outputs=["unused"])
            output = OutputManager("workflow", base_dir=tmp_path)

            with (
                patch.dict("os.environ", {"EMPTY_EXECUTOR_PROMPT": ""}, clear=False),
                pytest.raises(
                    NodeExecutionError,
                    match="Resolved executor prompt for node 'single.provider.empty.prompt' is empty after fragment assembly.",
                ),
            ):
                await execute_sequential_stage(
                    config,
                    node,
                    output,
                    invoker=invoker,
                )

            assert invoker.calls == []

    async def test_review_loop_fails_when_resolved_reviewer_prompt_is_empty(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )
            node = WorkflowNode(
                id="review.loop.empty.reviewer.prompt",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(
                        role=PromptSegmentRole.EXECUTOR,
                        content="Executor instructions.",
                    ),
                    PromptSegment(
                        role=ProviderRole.REVIEWER,
                        content="{{env:EMPTY_REVIEWER_PROMPT}}",
                    ),
                ],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review", role=ProviderRole.REVIEWER),
                ],
            )
            invoker = MockAgentInvoker(outputs=["unused"])
            output = OutputManager("workflow", base_dir=tmp_path)

            with (
                patch.dict("os.environ", {"EMPTY_REVIEWER_PROMPT": ""}, clear=False),
                pytest.raises(
                    NodeExecutionError,
                    match="Resolved reviewer prompt for node 'review.loop.empty.reviewer.prompt' is empty after fragment assembly.",
                ),
            ):
                await execute_sequential_stage(
                    config,
                    node,
                    output,
                    invoker=invoker,
                )

            assert invoker.calls == []

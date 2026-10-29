from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from crewplane.architecture.contracts import NodeArtifactRequest
from crewplane.artifacts import OutputManager
from crewplane.artifacts.workspace.state.fields import WorkspaceEvidenceRoot
from crewplane.artifacts.workspace.state.invocations import (
    ExpectedWorkspaceInvocation,
    expected_workspace_invocations,
)
from crewplane.core.config import AgentConfig, Config
from crewplane.core.preflight.compiled_records import ExecutionPolicy
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.runtime.execution import execute_sequential_stage
from crewplane.runtime.execution.common import CompiledRuntimeContext
from crewplane.version import SCHEMA_VERSION
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    compile_test_plan,
)


def compile_single_provider_plan(
    tmp_path: Path, depth: int | None
) -> tuple[OutputManager, PreflightExecutionPlan, CompiledRuntimeContext]:
    config = Config(
        version=SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["mock"])},
    )
    workflow = WorkflowPlan(
        name="single-provider-rounds",
        nodes=[
            WorkflowNode(
                id="single.provider.rounds",
                mode="sequential",
                depth=depth,
                prompt_segments=[PromptSegment(role="shared", content="run")],
                providers=[ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)],
            )
        ],
    )
    output = OutputManager(workflow.name, base_dir=tmp_path)
    plan, context = compile_test_plan(config, workflow, output)
    return output, plan, context


@pytest.mark.parametrize(("depth", "rounds"), [(None, 1), (1, 1), (3, 3)])
def test_execution_and_workspace_expectations_agree_on_final_round(
    tmp_path: Path, depth: int | None, rounds: int
) -> None:
    output, plan, context = compile_single_provider_plan(tmp_path, depth)
    node = plan.nodes[0]
    invoker = MockAgentInvoker(
        outputs=[f"round {round_num}" for round_num in range(1, rounds + 1)]
    )

    asyncio.run(execute_sequential_stage(node, output, context, invoker))

    assert len(invoker.calls) == rounds
    assert [call["round_num"] for call in invoker.calls] == list(range(1, rounds + 1))
    assert [call["audit_round_num"] for call in invoker.calls] == [None] * rounds
    node_dir = output.get_node_dir(NodeArtifactRequest(node.id, node.artifact_contract))
    assert node_dir is not None
    assert sorted(path.name for path in node_dir.glob("*.md")) == [
        f"alpha_executor_0_round{round_num}.md" for round_num in range(1, rounds + 1)
    ]
    expected = expected_workspace_invocations(
        WorkspaceEvidenceRoot(output.stages_dir), node
    )
    assert expected == (
        ExpectedWorkspaceInvocation(
            task_id="alpha_executor_0",
            role=ProviderRole.EXECUTOR,
            round_num=rounds,
            audit_round_num=None,
            lineage_source_required=True,
        ),
    )
    assert expected[0].round_num == invoker.calls[-1]["round_num"]
    assert (node_dir / f"alpha_executor_0_round{rounds}.md").read_text(
        encoding="utf-8"
    ) == f"round {rounds}"


@pytest.mark.parametrize("depth", [0, -2])
@pytest.mark.parametrize("bypass", ["construct", "mutate"])
def test_sequential_runtime_keeps_node_specific_guard_for_bypassed_policy(
    tmp_path: Path, depth: int, bypass: str
) -> None:
    output, plan, context = compile_single_provider_plan(tmp_path, None)
    node = plan.nodes[0]
    if bypass == "construct":
        node.execution_policy = ExecutionPolicy.model_construct(depth=depth)
    else:
        node.execution_policy.depth = depth
    invoker = MockAgentInvoker()

    with pytest.raises(ValueError) as caught:
        asyncio.run(execute_sequential_stage(node, output, context, invoker))

    assert str(caught.value) == (
        "Sequential node 'single.provider.rounds' depth must be greater than 0 when provided."
    )
    assert invoker.calls == []
    expected = expected_workspace_invocations(
        WorkspaceEvidenceRoot(output.stages_dir), node
    )
    assert expected[0].round_num == (depth or 1)

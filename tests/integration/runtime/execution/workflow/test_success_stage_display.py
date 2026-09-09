from __future__ import annotations

import asyncio
from pathlib import Path

from crewplane.architecture.contracts.artifacts import build_result_filename
from crewplane.artifacts import OutputManager
from crewplane.core.config import AgentConfig, Config
from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.version import SCHEMA_VERSION
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    TaskOutputInvoker,
    execute_workflow,
)


def test_repeated_provider_roles_have_distinct_ordered_result_headings(
    tmp_path: Path,
) -> None:
    config = Config(
        version=SCHEMA_VERSION,
        agents={
            name: AgentConfig(cli_cmd=["mock"], default_model="test")
            for name in ("worker", "solo")
        },
    )
    node = WorkflowNode(
        id="review",
        mode="sequential",
        providers=[
            ProviderSpec(provider="worker", role=ProviderRole.EXECUTOR),
            ProviderSpec(provider="solo", role=ProviderRole.EXECUTOR),
            ProviderSpec(provider="worker", role=ProviderRole.EXECUTOR),
            ProviderSpec(provider="worker", role=ProviderRole.REVIEWER),
            ProviderSpec(provider="worker", role=ProviderRole.REVIEWER),
        ],
        prompt_segments=[
            PromptSegment(role=PromptSegmentRole.SHARED, content="complete and review")
        ],
    )
    workflow = WorkflowPlan(name="provider.labels", nodes=[node])
    output = OutputManager(workflow.name, base_dir=tmp_path)
    invoker = TaskOutputInvoker({})

    asyncio.run(execute_workflow(config, workflow, output, invoker))

    result = (output.results_dir / build_result_filename(node.id)).read_text(
        encoding="utf-8"
    )
    headings = [
        line
        for line in result.splitlines()
        if line.startswith(("## worker (", "## solo ("))
    ]
    assert headings == [
        "## worker (executor 1)",
        "## solo (executor)",
        "## worker (executor 2)",
        "## worker (reviewer 1)",
        "## worker (reviewer 2)",
    ]
    assert len(invoker.calls) == 5
    for call in invoker.calls:
        if call["role"] == ProviderRole.EXECUTOR:
            assert f"output for {call['task_id']}" in result
    assert result.count("VERDICT: NO_FINDINGS") == 2

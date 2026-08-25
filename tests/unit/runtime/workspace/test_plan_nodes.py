from __future__ import annotations

import pytest

from crewplane.core.preflight import PreflightExecutionNode, PreflightExecutionPlan
from crewplane.runtime.workspace.plan_nodes import workspace_plan_node


def test_workspace_plan_node_returns_compiled_node() -> None:
    node = PreflightExecutionNode.model_construct(id="source")
    plan = PreflightExecutionPlan.model_construct(nodes=[node])

    assert workspace_plan_node(plan, "source") is node


def test_workspace_plan_node_preserves_unknown_source_error() -> None:
    plan = PreflightExecutionPlan.model_construct(nodes=[])

    with pytest.raises(
        RuntimeError,
        match="Workspace source references unknown node 'missing'\\.",
    ):
        workspace_plan_node(plan, "missing")

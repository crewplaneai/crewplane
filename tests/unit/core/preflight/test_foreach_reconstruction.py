import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from crewplane.core.preflight.foreach.expansion import (
    descriptor,
    instantiate_foreach,
    scheduling_edge,
)
from crewplane.core.preflight.foreach.reconstruction import (
    apply_expansion,
    topological_order,
    validate_expansion_provenance,
    validate_frozen_templates,
)
from crewplane.core.preflight.models import PreflightExecutionPlan
from tests.helpers.foreach import compile_foreach_file, copy_foreach_fixture


@pytest.fixture
def plan(tmp_path: Path) -> PreflightExecutionPlan:
    preview = compile_foreach_file(copy_foreach_fixture(tmp_path))
    assert not preview.has_errors(), preview.diagnostics
    return PreflightExecutionPlan.from_preview(
        preview,
        "run",
        "run",
        str(tmp_path),
        str(tmp_path),
        str(tmp_path),
        datetime(2026, 10, 28, tzinfo=UTC),
    )


@pytest.mark.parametrize("items", [[], [{"chunk": 1}, {"chunk": 2}]])
def test_apply_expansion_preserves_order_and_copy_boundaries(
    plan: PreflightExecutionPlan, items: list[dict[str, int]]
) -> None:
    batch = instantiate_foreach(plan, plan.foreach_templates[0], items, {})
    before_plan, before_batch = plan.model_dump(), batch.model_dump()

    result = apply_expansion(plan, batch)

    assert result is not plan
    assert result.nodes is not plan.nodes
    assert [node.id for node in result.nodes] == [
        "request",
        "planner",
        *(node.id for node in batch.nodes),
        "chunks",
        "summarize",
    ]
    assert result.execution_order == [node.id for node in result.nodes]
    assert result.nodes[0] is plan.nodes[0]
    assert result.nodes[1] is plan.nodes[1]
    assert result.nodes[-1] is plan.nodes[-1]
    assert all(
        node is generated
        for node, generated in zip(result.nodes[2:-2], batch.nodes, strict=True)
    )
    group = result.nodes[-2]
    assert group is not plan.nodes[2]
    assert group.dependencies is batch.group_dependencies
    assert group.expansion == batch.state.provenance()
    assert result.expansions == {"chunks": batch.state}
    assert result.expansions["chunks"] is batch.state
    assert result.dependency_graph == [
        *plan.dependency_graph,
        *batch.dependency_graph,
        *(
            scheduling_edge(dependency, "chunks")
            for dependency in batch.group_dependencies
            if dependency != "planner"
        ),
    ]
    assert result.render_plans == [*plan.render_plans, *batch.render_plans]
    assert result.workspace_file_locators == [
        *plan.workspace_file_locators,
        *batch.workspace_file_locators,
    ]
    assert result.token_catalog == [*plan.token_catalog, *batch.token_catalog]
    assert result.foreach_templates is plan.foreach_templates
    assert result.static_resources is plan.static_resources
    assert result.runtime_config_snapshot is plan.runtime_config_snapshot
    assert plan.model_dump() == before_plan
    assert batch.model_dump() == before_batch
    assert validate_frozen_templates(result) is None
    assert validate_expansion_provenance(result) is None


def test_duplicate_expansion_precedes_batch_validation(
    plan: PreflightExecutionPlan,
) -> None:
    batch = instantiate_foreach(plan, plan.foreach_templates[0], [{}], {})
    result = apply_expansion(plan, batch)
    batch.nodes.clear()
    before = result.model_dump()
    with pytest.raises(ValueError, match="Duplicate expansion 'chunks'"):
        apply_expansion(result, batch)
    assert result.model_dump() == before


@pytest.mark.parametrize(
    "damage,message",
    [
        ("duplicates", "duplicate foreach template"),
        ("missing", "exactly one frozen child template"),
        ("budget", "compiled positive max_expanded_nodes"),
        ("concurrency", "finite node concurrency"),
        ("bound", "Frozen foreach bounds exceed max_expanded_nodes"),
        ("collection", "numbers must be finite"),
        ("identity", "Frozen child template identity changed"),
        ("child_ids", "duplicate child node"),
        ("order", "execution_order must contain every node exactly once"),
        ("slot", "exactly one declared item input slot"),
        ("nested", "invalid node identity, expansion, or nested foreach"),
        ("graph", "invalid render_plan_id"),
    ],
)
def test_frozen_template_validation_order(
    plan: PreflightExecutionPlan, damage: str, message: str
) -> None:
    template = plan.foreach_templates[0]
    execution = plan.runtime_config_snapshot["execution"]
    if damage == "duplicates":
        plan.foreach_templates.append(template)
        plan.nodes.clear()
    elif damage == "missing":
        plan.foreach_templates.clear()
        execution["max_expanded_nodes"] = 0
    elif damage == "budget":
        execution["max_expanded_nodes"] = 0
        execution["max_concurrent_nodes"] = None
    elif damage == "concurrency":
        execution["max_concurrent_nodes"] = True
        execution["max_expanded_nodes"] = 1
    elif damage == "bound":
        execution["max_expanded_nodes"] = 1
        template.items = [{"bad": float("nan")}]
    elif damage == "collection":
        template.items = [{"bad": float("nan")}]
        template.namespace = "changed"
    elif damage == "identity":
        template.namespace = "changed"
        template.nodes.append(template.nodes[0])
    else:
        if damage == "child_ids":
            template.nodes.append(template.nodes[0])
            template.execution_order.clear()
        elif damage == "order":
            template.execution_order.clear()
            template.item_node_id = "missing"
        elif damage == "slot":
            template.item_node_id = "missing"
            template.nodes[-1].mode = "foreach"
        elif damage == "nested":
            template.nodes[-1].mode = "foreach"
        else:
            template.render_plans.clear()
        template.template_digest = template.semantic_digest()
    before = plan.model_dump_json()
    with pytest.raises(ValueError, match=message):
        validate_frozen_templates(plan)
    assert plan.model_dump_json() == before


def test_no_foreach_templates_require_no_expansion_settings(
    plan: PreflightExecutionPlan,
) -> None:
    plan.nodes = [node for node in plan.nodes if node.mode != "foreach"]
    plan.foreach_templates.clear()
    plan.runtime_config_snapshot.clear()
    before = plan.model_dump()
    assert validate_frozen_templates(plan) is None
    assert plan.model_dump() == before


@pytest.mark.parametrize(
    "damage,message",
    [
        ("identity", "frozen template and root signature"),
        ("locators", "item count or locators"),
        ("digest", "digest does not match its evidence"),
        ("gate", "completion gate differs from its instance terminals"),
        ("generated", "differs from its frozen template"),
        ("budget", "Restored expansions exceed max_expanded_nodes"),
        ("provenance", "invalid expansion provenance"),
        ("slot", "Uninstantiated item slots"),
    ],
)
def test_provenance_validation_order(
    plan: PreflightExecutionPlan, damage: str, message: str
) -> None:
    template = plan.foreach_templates[0]
    result = apply_expansion(plan, instantiate_foreach(plan, template, [{}], {}))
    state = result.expansions["chunks"]
    group = next(node for node in result.nodes if node.id == "chunks")
    if damage == "identity":
        state.workflow_signature = "changed"
        state.collection = descriptor("wrong/items.json", b"[]")
    elif damage == "locators":
        state.items[0] = descriptor("wrong/item.json", b"{}")
        state.digest = "0" * 64
    elif damage == "digest":
        state.digest = "0" * 64
        group.dependencies.clear()
    elif damage == "gate":
        group.dependencies.clear()
        result.nodes[2].dependencies.clear()
    elif damage == "generated":
        result.nodes[2].dependencies.clear()
        result.runtime_config_snapshot["execution"]["max_expanded_nodes"] = 1
    elif damage == "budget":
        result.runtime_config_snapshot["execution"]["max_expanded_nodes"] = 1
        group.expansion = None
    elif damage == "provenance":
        group.expansion = None
        group.input_item_slot = True
    else:
        result.nodes[0].input_item_slot = True
    before = result.model_dump()
    with pytest.raises(ValueError, match=message):
        validate_expansion_provenance(result)
    assert result.model_dump() == before


def test_topological_order_uses_input_position_for_ready_nodes(
    plan: PreflightExecutionPlan,
) -> None:
    prototype = plan.nodes[0]
    nodes = [
        prototype.model_copy(update={"id": node_id, "dependencies": dependencies})
        for node_id, dependencies in [
            ("dependent", ["root", "root"]),
            ("root", []),
            ("independent", []),
        ]
    ]
    before = [node.model_dump() for node in nodes]
    assert topological_order(nodes) == ["root", "dependent", "independent"]
    assert [node.model_dump() for node in nodes] == before
    assert topological_order([]) == []


@pytest.mark.parametrize(
    "records,message",
    [
        (
            [("duplicate", ["unknown"]), ("duplicate", [])],
            "Expansion contains colliding node identities",
        ),
        (
            [("cycle", ["cycle"]), ("dependent", ["unknown"])],
            "Expansion references unknown dependency 'unknown'",
        ),
        ([("cycle", ["cycle"])], "Expansion introduces a dependency cycle"),
    ],
)
def test_topological_order_error_precedence(
    plan: PreflightExecutionPlan,
    records: list[tuple[str, list[str]]],
    message: str,
) -> None:
    nodes = [
        plan.nodes[0].model_copy(update={"id": node_id, "dependencies": dependencies})
        for node_id, dependencies in records
    ]
    before = [node.model_dump() for node in nodes]
    with pytest.raises(ValueError, match=re.escape(message)):
        topological_order(nodes)
    assert [node.model_dump() for node in nodes] == before

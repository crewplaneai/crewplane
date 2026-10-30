import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from crewplane.core.preflight.foreach.expansion import instantiate_foreach
from crewplane.core.preflight.foreach.reconstruction import apply_expansion
from crewplane.core.preflight.models import PreflightExecutionPlan
from tests.helpers.foreach import (
    compile_foreach_file,
    compile_literal_foreach,
    copy_foreach_fixture,
    foreach_config,
    workflow_document,
    write_workflow_document,
)


def test_frozen_child_slots_expand_and_gate_all_roots(tmp_path: Path) -> None:
    preview = compile_literal_foreach(tmp_path)
    assert not preview.has_errors(), preview.diagnostics
    assert [node.id for node in preview.nodes] == ["chunks"]
    template = preview.foreach_templates[0]
    assert template.node_bound == 6
    assert not preview.static_resources
    plan = PreflightExecutionPlan.from_preview(
        preview,
        "run",
        "run",
        str(tmp_path),
        str(tmp_path),
        str(tmp_path),
        datetime.now(UTC),
    )
    batch = instantiate_foreach(plan, template, [{"x": 1}, {"x": 1}], {})
    effective = apply_expansion(plan, batch)
    nodes = {node.id: node for node in effective.nodes}
    assert nodes["chunks.i0.independent"].dependencies == ["chunks.i0.item"]
    assert nodes["chunks.i1.analyze"].dependencies == ["chunks.i1.item"]
    assert set(nodes["chunks"].dependencies) == {
        "chunks.i0.analyze",
        "chunks.i0.independent",
        "chunks.i1.analyze",
        "chunks.i1.independent",
    }
    assert plan.nodes[0].dependencies == []
    assert len(effective.nodes) == 7


@pytest.mark.parametrize("items", ["[]", "[{}]"])
@pytest.mark.parametrize("max_items", [None, 10])
def test_empty_and_single_collections_validate_without_fallback_files(
    tmp_path: Path, items: str, max_items: int | None
) -> None:
    preview = compile_literal_foreach(tmp_path, items, max_items)
    assert not preview.has_errors(), preview.diagnostics
    template = preview.foreach_templates[0]
    assert template.max_items == (
        max_items if max_items is not None else len(json.loads(items))
    )
    assert len(template.nodes) == 3


@pytest.mark.parametrize("max_items", [None, 10])
def test_literal_budget_uses_exact_length_including_input_nodes(
    tmp_path: Path, max_items: int | None
) -> None:
    rejected = compile_literal_foreach(
        tmp_path, max_items=max_items, max_expanded_nodes=5
    )
    assert rejected.has_errors()
    assert "bound 6" in str(rejected.diagnostics)
    accepted = compile_literal_foreach(
        tmp_path, max_items=max_items, max_expanded_nodes=6
    )
    assert not accepted.has_errors(), accepted.diagnostics
    assert accepted.foreach_templates[0].node_bound == 6


def test_literal_assertion_rejects_overflow_before_compilation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="2 items exceeds max_items 1"):
        compile_literal_foreach(tmp_path, max_items=1)


@pytest.mark.parametrize("include_literal", [False, True])
def test_dynamic_and_mixed_group_budgets(tmp_path: Path, include_literal: bool) -> None:
    path = copy_foreach_fixture(tmp_path)
    payload, body = workflow_document(path)
    if include_literal:
        literal = {**payload["nodes"][2], "id": "literal", "items": [{}]}
        del literal["max_items"]
        payload["nodes"].append(literal)
        write_workflow_document(path, payload, body + "\n## literal\n")
    bound = 10 if include_literal else 8
    rejected = compile_foreach_file(path, foreach_config(max_expanded_nodes=bound - 1))
    assert rejected.has_errors()
    assert f"bound {bound}" in str(rejected.diagnostics)
    accepted = compile_foreach_file(path, foreach_config(max_expanded_nodes=bound))
    assert not accepted.has_errors(), accepted.diagnostics
    assert sum(template.node_bound for template in accepted.foreach_templates) == bound


@pytest.mark.parametrize(
    "settings, message",
    [({"max_concurrent_nodes": None}, "finite"), ({"max_expanded_nodes": 5}, "bound")],
)
def test_preflight_bounds(
    tmp_path: Path, settings: dict[str, object], message: str
) -> None:
    preview = compile_literal_foreach(tmp_path, **settings)
    assert preview.has_errors()
    assert message in str(preview.diagnostics)


@pytest.mark.parametrize("value", [True, 0, -1, "10", 10.5])
def test_expansion_setting_is_strict(value: object) -> None:
    with pytest.raises(ValueError):
        foreach_config(max_expanded_nodes=value)


@pytest.mark.parametrize(
    "change",
    ["provider", "prompt", "namespace", "source", "ancestor", "multi_findings"],
)
def test_preflight_validates_entire_procedure_and_collection_sources(
    tmp_path: Path, change: str
) -> None:
    path = copy_foreach_fixture(tmp_path)
    payload, body = workflow_document(path)
    child, child_body = workflow_document(tmp_path / "analyze.md")
    if change == "provider":
        payload["nodes"][2]["items"] = []
        del payload["nodes"][2]["max_items"]
        child["nodes"][-1]["providers"] = ["missing"]
    elif change == "prompt":
        payload["nodes"][2]["items"] = []
        del payload["nodes"][2]["max_items"]
        child_body += "\n{{file:missing-context.txt}}"
    elif change == "namespace":
        payload["nodes"].append(
            {"id": "chunks.authored", "mode": "parallel", "providers": ["alpha"]}
        )
        body += "\n## chunks.authored\nBad reservation."
    elif change == "source":
        payload["nodes"][2]["items"] = "{{planner.output}}"
    elif change == "ancestor":
        payload["nodes"][2]["needs"] = []
    else:
        payload["nodes"][1]["providers"] = ["alpha", "alpha"]
    write_workflow_document(path, payload, body)
    write_workflow_document(tmp_path / "analyze.md", child, child_body)
    preview = compile_foreach_file(path)
    assert preview.has_errors(), change


@pytest.mark.parametrize("change", ["child", "literal", "binding", "parameter"])
def test_root_signature_covers_frozen_foreach_semantics(
    tmp_path: Path, change: str
) -> None:
    path = copy_foreach_fixture(tmp_path)
    before = compile_foreach_file(path)
    payload, body = workflow_document(path)
    if change == "child":
        child, child_body = workflow_document(tmp_path / "analyze.md")
        write_workflow_document(
            tmp_path / "analyze.md", child, child_body + "\nBe precise."
        )
    elif change == "literal":
        payload["nodes"][2]["items"] = [{"chunk": "literal"}]
    elif change == "binding":
        payload["nodes"][2]["inputs"]["request"] = "planner"
    else:
        payload["nodes"][2]["with"]["style"] = "detailed"
    write_workflow_document(path, payload, body)
    after = compile_foreach_file(path)
    assert not after.has_errors(), after.diagnostics
    assert before.workflow_signature != after.workflow_signature


def test_child_repetition_and_ignored_fallback_do_not_change_signature(
    tmp_path: Path,
) -> None:
    path = copy_foreach_fixture(tmp_path)
    before = compile_foreach_file(path)
    child, body = workflow_document(tmp_path / "child.md")
    child["repeat_force_run_count"] = 5
    child["nodes"][0]["source"] = "{{file:another-ignored-file.json}}"
    write_workflow_document(tmp_path / "child.md", child, body)
    after = compile_foreach_file(path)
    assert not after.has_errors(), after.diagnostics
    assert before.workflow_signature == after.workflow_signature


@pytest.mark.parametrize("change", ["contents", "order", "explicit_cap"])
def test_literal_signature_preserves_authored_identity(
    tmp_path: Path, change: str
) -> None:
    before = compile_literal_foreach(tmp_path, "[{x: 1}, {x: 2}]")
    path = tmp_path / "parent.md"
    assert compile_foreach_file(path).workflow_signature == before.workflow_signature
    payload, body = workflow_document(path)
    group = payload["nodes"][0]
    if change == "contents":
        group["items"][0]["x"] = 3
    elif change == "order":
        group["items"].reverse()
    else:
        group["max_items"] = 2
    write_workflow_document(path, payload, body)
    after = compile_foreach_file(path)
    assert not after.has_errors(), after.diagnostics
    assert after.workflow_signature != before.workflow_signature
    if change == "explicit_cap":
        del group["max_items"]
        write_workflow_document(path, payload, body)
        assert (
            compile_foreach_file(path).workflow_signature == before.workflow_signature
        )


def test_literal_projected_index_rejects_collection_near_byte_limit(
    tmp_path: Path,
) -> None:
    preview = compile_literal_foreach(
        tmp_path, json.dumps([{"data": "x" * (1024 * 1024 - 20)}])
    )
    assert preview.has_errors()
    assert "projected result index" in str(preview.diagnostics)


def test_projected_index_accounts_for_item_nesting(tmp_path: Path) -> None:
    item = {"value": 0}
    for depth in range(62):
        item = {f"level{depth}": item}
    preview = compile_literal_foreach(tmp_path, json.dumps([item]))
    assert preview.has_errors()
    assert "projected result index" in str(preview.diagnostics)
    assert "depth" in str(preview.diagnostics)


def test_multiple_groups_and_restored_expansions_share_one_budget(
    tmp_path: Path,
) -> None:
    compile_literal_foreach(tmp_path, "[{}]")
    path = tmp_path / "parent.md"
    payload, body = workflow_document(path)
    payload["nodes"].append({**payload["nodes"][0], "id": "other"})
    write_workflow_document(path, payload, body + "\n## other\n")
    rejected = compile_foreach_file(path, foreach_config(max_expanded_nodes=5))
    assert rejected.has_errors()
    assert "bound 6" in str(rejected.diagnostics)
    preview = compile_foreach_file(path, foreach_config(max_expanded_nodes=6))
    assert not preview.has_errors(), preview.diagnostics
    plan = PreflightExecutionPlan.from_preview(
        preview,
        "run",
        "run",
        str(tmp_path),
        str(tmp_path),
        str(tmp_path),
        datetime.now(UTC),
    )
    first, second = plan.foreach_templates
    restored = apply_expansion(plan, instantiate_foreach(plan, first, [{}], {}))
    assert len(instantiate_foreach(restored, second, [{}], {}).nodes) == 3
    # Exercise the runtime guard independently of the earlier static-bound check.
    restored.runtime_config_snapshot["execution"]["max_expanded_nodes"] = 5
    with pytest.raises(ValueError, match="cumulative max_expanded_nodes"):
        instantiate_foreach(restored, second, [{}], {})


@pytest.mark.parametrize("field", ["gate", "node_id", "locator", "render"])
def test_concrete_batch_cannot_override_the_frozen_procedure(
    tmp_path: Path, field: str
) -> None:
    preview = compile_literal_foreach(tmp_path, "[{}]")
    plan = PreflightExecutionPlan.from_preview(
        preview,
        "run",
        "run",
        str(tmp_path),
        str(tmp_path),
        str(tmp_path),
        datetime.now(UTC),
    )
    batch = instantiate_foreach(plan, plan.foreach_templates[0], [{}], {})
    if field == "gate":
        batch.nodes[1].dependencies.clear()
    elif field == "node_id":
        batch.nodes[1].id = batch.nodes[0].id
    elif field == "locator":
        batch.nodes[1].artifact_contract.output_path = batch.nodes[
            0
        ].artifact_contract.output_path
    else:
        batch.render_plans[0].streams.clear()
    with pytest.raises(ValueError, match="frozen child template"):
        apply_expansion(plan, batch)


@pytest.mark.parametrize("field", ["input_item_slot", "input_content_ref"])
def test_persisted_foreach_control_nodes_reject_input_execution_fields(
    tmp_path: Path, field: str
) -> None:
    preview = compile_literal_foreach(tmp_path, "[]")
    plan = PreflightExecutionPlan.from_preview(
        preview,
        "run",
        "run",
        str(tmp_path),
        str(tmp_path),
        str(tmp_path),
        datetime.now(UTC),
    )
    payload = plan.model_dump()
    payload["nodes"][0][field] = (
        True if field == "input_item_slot" else "preflight/items.json"
    )
    with pytest.raises(ValueError, match="item slots|input_content_ref"):
        PreflightExecutionPlan.model_validate(payload)

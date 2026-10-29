import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from crewplane.core.preflight.compiled_records import (
    Fragment,
    RenderStream,
    TokenCatalogEntry,
    WorkspaceFileLocator,
)
from crewplane.core.preflight.dependency_edges import dependency_signature
from crewplane.core.preflight.foreach.expansion import (
    concrete_batch,
    descriptor,
    instantiate_foreach,
    scheduling_edge,
)
from crewplane.core.preflight.foreach.index import terminal_nodes
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.preflight.serialization import canonical_json_bytes
from crewplane.core.preflight.signatures import signature_for_payload
from crewplane.core.preflight.workspace.models import WorkspaceSelectionRecord
from tests.helpers.foreach import compile_literal_foreach


@pytest.fixture
def plan(tmp_path: Path) -> PreflightExecutionPlan:
    preview = compile_literal_foreach(tmp_path)
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


@pytest.mark.parametrize(
    "items,expanded,limit,message",
    [
        ([{"bad": float("nan")}], True, False, "numbers must be finite"),
        ([{}] * 11, True, False, "11 items exceeds max_items 10"),
        ([{}], True, False, "is already expanded"),
        ([{}], False, False, "compiled positive max_expanded_nodes"),
        ([{}], False, 2, "exceeds cumulative max_expanded_nodes"),
    ],
)
def test_expansion_validation_order(
    plan: PreflightExecutionPlan,
    items: list[dict[str, object]],
    expanded: bool,
    limit: int | bool,
    message: str,
) -> None:
    template = plan.foreach_templates[0]
    if expanded:
        plan.expansions[template.group_id] = instantiate_foreach(
            plan, template, [{}], {}
        ).state
    plan.runtime_config_snapshot["execution"]["max_expanded_nodes"] = limit
    before = plan.model_dump()
    with pytest.raises(ValueError, match=message):
        instantiate_foreach(plan, template, items, {})
    assert plan.model_dump() == before


def test_expansion_identity_uses_canonical_bytes_and_caller_evidence(
    plan: PreflightExecutionPlan,
) -> None:
    template = plan.foreach_templates[0]
    items = [{"z": "é", "a": 1}, {"z": "é", "a": 1}]
    callers = {"request.output": descriptor("request-result.md", b"request")}
    before = plan.model_dump()
    state = instantiate_foreach(plan, template, items, callers).state
    assert state.collection.relative_path == "preflight/expansions/chunks/items.json"
    assert state.collection.size_bytes == len(canonical_json_bytes(items))
    assert (
        state.collection.sha256
        == hashlib.sha256(canonical_json_bytes(items)).hexdigest()
    )
    assert [item.relative_path for item in state.items] == [
        "preflight/expansions/chunks/items/0.json",
        "preflight/expansions/chunks/items/1.json",
    ]
    assert state.items[0].sha256 == state.items[1].sha256
    assert state.items[0].size_bytes == len(canonical_json_bytes(items[0]))
    assert state.digest == signature_for_payload(
        {
            "workflow_signature": plan.workflow_signature,
            "template_digest": template.template_digest,
            "group_id": template.group_id,
            "collection": state.collection,
            "items": state.items,
            "caller_artifacts": callers,
            "shared_bindings": template.shared_bindings,
        }
    )
    assert state.caller_artifacts == callers
    assert state.caller_artifacts is not callers
    assert state.caller_artifacts["request.output"] is callers["request.output"]
    assert plan.model_dump() == before


def test_concrete_batch_preserves_node_edge_order_and_copy_boundaries(
    plan: PreflightExecutionPlan,
) -> None:
    template = plan.foreach_templates[0].model_copy(deep=True)
    nodes = {node.id.rsplit(".", 1)[-1]: node for node in template.nodes}
    item, analyze, independent = nodes["item"], nodes["analyze"], nodes["independent"]
    template.nodes = [item, analyze, independent]
    template.original_dependencies = ["caller", "caller"]
    analyze.dependencies = [item.id, "caller", item.id]
    independent.dependencies = ["caller"]
    independent.workspace_policy = None
    analyze.workspace_policy = WorkspaceSelectionRecord(
        enabled=True,
        logical_worktree_name=analyze.id,
        declaration_kind="worktree",
        source_kind="node",
        source_node_id=item.id,
        materialization="worktree_checkout",
        writable=True,
    )
    template.execution_order = [item.id, independent.id, analyze.id]
    state = instantiate_foreach(plan, template, [{}, {}], {}).state
    dependencies = ["caller", "caller"]
    before = template.model_dump()
    batch = concrete_batch(template, state, dependencies)

    assert [node.id for node in batch.nodes] == [
        "chunks.i0.item",
        "chunks.i0.analyze",
        "chunks.i0.independent",
        "chunks.i1.item",
        "chunks.i1.analyze",
        "chunks.i1.independent",
    ]
    assert batch.group_dependencies == [
        "caller",
        "caller",
        "chunks.i0.analyze",
        "chunks.i0.independent",
        "chunks.i1.analyze",
        "chunks.i1.independent",
    ]
    assert batch.group_dependencies is not dependencies
    assert terminal_nodes(template) == [independent.id, analyze.id]
    assert [node.dependencies for node in batch.nodes[:3]] == [
        ["caller"],
        ["chunks.i0.item", "caller"],
        ["caller", "chunks.i0.item"],
    ]
    assert [
        (edge.source_node, edge.target_node, edge.artifact_name)
        for edge in batch.dependency_graph[:6]
    ] == [
        ("caller", "chunks.i0.item", None),
        ("chunks.i0.item", "chunks.i0.analyze", None),
        ("caller", "chunks.i0.analyze", None),
        ("caller", "chunks.i0.independent", None),
        ("chunks.i0.item", "chunks.i0.independent", None),
        ("chunks.i0.item", "chunks.i0.analyze", "output"),
    ]
    generated = batch.nodes[1]
    assert generated.workspace_policy is not analyze.workspace_policy
    assert generated.workspace_policy is not None
    assert generated.workspace_policy.logical_worktree_name == "chunks.i0.analyze"
    assert generated.workspace_policy.source_node_id == "chunks.i0.item"
    assert (
        generated.workspace_policy.branch_export
        is analyze.workspace_policy.branch_export
    )
    assert generated.provider_records is analyze.provider_records
    assert generated.execution_policy is analyze.execution_policy
    assert generated.artifact_contract is not analyze.artifact_contract
    assert generated.render_plan_id == generated.id
    assert batch.nodes[2].workspace_policy is None
    assert not batch.nodes[0].input_item_slot
    assert batch.nodes[0].input_content_ref == state.items[0].relative_path
    assert generated.expansion == state.provenance(0)
    assert batch.state is state
    assert template.model_dump() == before

    template.nodes = [item]
    template.execution_order = [item.id]
    template.dependency_graph = []
    template.render_plans = []
    template.token_catalog = []
    assert terminal_nodes(template) == [item.id]
    assert concrete_batch(template, state, []).group_dependencies == [
        "chunks.i0.item",
        "chunks.i1.item",
    ]


def test_concrete_batch_remaps_render_and_token_locators_without_mutation(
    plan: PreflightExecutionPlan,
) -> None:
    template = plan.foreach_templates[0].model_copy(deep=True)
    node = next(node for node in template.nodes if node.id.endswith(".analyze"))
    locator = WorkspaceFileLocator(
        locator_id="workspace-file-original",
        occurrence_id=f"{node.id}:executor:1",
        node_id=node.id,
        target="executor_prompt",
        source_class="runtime_dynamic",
        raw_token="{{file:context.txt}}",
        raw_path="context.txt",
        source_root="/project",
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="context.txt",
        workspace_relative_path="context.txt",
    )
    template.workspace_file_locators = [locator]
    render = next(
        render for render in template.render_plans if render.node_id == node.id
    )
    template.render_plans = [render]
    fragments = [
        Fragment(fragment_index=0, kind="literal", source_role="shared", text="Use "),
        Fragment(
            fragment_index=1,
            kind="workspace_file_locator",
            source_role="shared",
            locator={"locator_id": locator.locator_id},
        ),
        *render.streams[0].fragments,
        Fragment(
            fragment_index=5,
            kind="runtime_locator_lookup",
            source_role="shared",
            locator={"node_id": "caller", "artifact_name": "findings"},
        ),
    ]
    render.streams = [RenderStream(target_role="executor", fragments=fragments)]
    file_token = TokenCatalogEntry(
        occurrence_id=locator.occurrence_id,
        node_id=node.id,
        target_role="executor",
        source_role="shared",
        raw_token=locator.raw_token,
        token_kind="file",
        fragment_index=1,
        signature="file-signature",
        canonical_locator=locator.locator_id,
        resolved={"locator_id": locator.locator_id, "node_id": node.id},
        metadata={"locator_id": locator.locator_id, "node_id": node.id},
    )
    node_token = template.token_catalog[0]
    external_token = node_token.model_copy(
        update={
            "raw_token": "{{caller.findings}}",
            "metadata": {"node_id": "caller", "artifact_name": "findings"},
        }
    )
    unchanged_token = file_token.model_copy(
        update={"canonical_locator": "static-file", "metadata": {}}
    )
    template.token_catalog = [node_token, file_token, external_token, unchanged_token]
    template.dependency_graph.append(scheduling_edge("caller", node.id, "findings"))
    state = instantiate_foreach(plan, template, [{}], {}).state
    before = template.model_dump()
    batch = concrete_batch(template, state, [])

    generated_locator = batch.workspace_file_locators[0]
    expected_locator_id = "workspace-file-" + signature_for_payload(
        [locator.locator_id, "chunks.i0.item"]
    )
    assert generated_locator.locator_id == expected_locator_id
    assert generated_locator.node_id == "chunks.i0.analyze"
    assert generated_locator.occurrence_id == "chunks.i0.analyze:executor:1"
    generated_render = batch.render_plans[0]
    assert (
        generated_render.node_id
        == generated_render.render_plan_id
        == "chunks.i0.analyze"
    )
    assert generated_render.template_signature == signature_for_payload(
        {"node_id": "chunks.i0.analyze", "template_hash": render.template_hash}
    )
    rewritten = generated_render.streams[0].fragments
    assert rewritten[0] == fragments[0]
    assert rewritten[0] is not fragments[0]
    assert rewritten[1].locator == {"locator_id": expected_locator_id}
    assert [
        fragment.locator
        for fragment in rewritten
        if fragment.kind == "runtime_locator_lookup"
    ] == [
        {"node_id": "chunks.i0.item", "artifact_name": "output"},
        {"node_id": "caller", "artifact_name": "findings"},
    ]
    for original, copied in zip(fragments, rewritten, strict=True):
        if original.locator is not None:
            assert copied.locator is not original.locator
    for index, source, artifact in [
        (0, "chunks.i0.item", "output"),
        (2, "caller", "findings"),
    ]:
        token = batch.token_catalog[index]
        signature = dependency_signature(source, "chunks.i0.analyze", artifact)
        assert token.raw_token == "{{" + source + "." + artifact + "}}"
        assert token.canonical_locator == f"{source}.{artifact}"
        assert token.dependency_signature == signature
        assert token.resolved["locator"] == {
            "node_id": source,
            "artifact_name": artifact,
        }
        assert token.metadata["dependency_signature"] == signature
    generated_file = batch.token_catalog[1]
    assert generated_file.canonical_locator == expected_locator_id
    assert (
        generated_file.resolved
        == generated_file.metadata
        == {
            "locator_id": expected_locator_id,
            "node_id": "chunks.i0.analyze",
        }
    )
    assert batch.token_catalog[3].resolved == unchanged_token.resolved
    assert batch.token_catalog[3].canonical_locator == "static-file"
    for original, copied in zip(
        template.token_catalog, batch.token_catalog, strict=True
    ):
        assert copied.occurrence_id == original.occurrence_id.replace(
            node.id, "chunks.i0.analyze", 1
        )
        assert copied.signature == signature_for_payload(
            [original.signature, "chunks.i0.analyze"]
        )
        assert copied.resolved is not original.resolved
        assert copied.metadata is not original.metadata
    assert batch.dependency_graph[-1] == scheduling_edge(
        "caller", "chunks.i0.analyze", "findings"
    )
    assert template.model_dump() == before

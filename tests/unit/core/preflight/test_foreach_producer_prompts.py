from pathlib import Path

import pytest
from rich.console import Console

from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.preflight import PreflightCompileOptions, compile_preflight_preview
from crewplane.core.preflight.compile_state import CompileState
from crewplane.core.preflight.foreach.prompts import with_collection_constraints
from crewplane.core.preflight.models import PreflightCompilationPreview
from crewplane.core.preflight.render_plans import compile_render_plan
from crewplane.core.preflight.source import (
    PreflightWorkflowSource,
    load_workflow_source_for_preflight,
)
from crewplane.core.prompt_segments import PromptSegment
from tests.helpers.foreach import (
    compile_foreach_file,
    copy_foreach_fixture,
    foreach_config,
    workflow_document,
    write_workflow_document,
)


def test_dynamic_cap_reaches_both_streams_and_changes_prompt_identity(
    tmp_path: Path,
) -> None:
    path = copy_foreach_fixture(tmp_path)
    source = load_workflow_source_for_preflight(path, tmp_path)
    previews = []
    for limit in (12, 2):
        source.workflow.nodes[2].max_items = limit
        preview = compile_source(source, tmp_path)
        assert not preview.has_errors(), preview.diagnostics
        previews.append(preview)
        render = next(
            plan for plan in preview.render_plans if plan.node_id == "planner"
        )
        assert {stream.target_role for stream in render.streams} == {
            "executor",
            "reviewer",
        }
        for stream in render.streams:
            fragment = stream.fragments[-1]
            assert fragment.kind == "literal"
            assert fragment.source_role == "shared"
            assert fragment.text == (
                f"\n\nExecutor findings: JSON array with at most {limit} items.\n"
            )
    first, second = previews
    before = next(plan for plan in first.render_plans if plan.node_id == "planner")
    after = next(plan for plan in second.render_plans if plan.node_id == "planner")
    assert before.template_hash != after.template_hash
    assert before.template_signature != after.template_signature
    assert first.workflow_signature != second.workflow_signature
    assert first.token_catalog == second.token_catalog
    assert first.dependency_graph == second.dependency_graph
    assert (
        first.foreach_templates[0].render_plans
        == second.foreach_templates[0].render_plans
    )
    assert first.render_plans[1:] == second.render_plans[1:]


@pytest.mark.parametrize("caps", [(12, 5), (5, 12)])
def test_shared_consumers_use_minimum_without_replacing_group_caps(
    tmp_path: Path, caps: tuple[int, int]
) -> None:
    path = copy_foreach_fixture(tmp_path)
    payload, body = workflow_document(path)
    group = payload["nodes"][2]
    group["max_items"] = caps[0]
    payload["nodes"].append({**group, "id": "other", "max_items": caps[1]})
    write_workflow_document(path, payload, body + "\n## other\n")
    first = compile_foreach_file(path)
    assert not first.has_errors(), first.diagnostics
    assert [template.max_items for template in first.foreach_templates] == list(caps)
    render = next(plan for plan in first.render_plans if plan.node_id == "planner")
    for stream in render.streams:
        text = "".join(fragment.text or "" for fragment in stream.fragments)
        assert text.count("Executor findings:") == 1
        assert "at most 5 items." in text
    for node in payload["nodes"]:
        if node.get("max_items") == 12:
            node["max_items"] = 10
    write_workflow_document(path, payload, body + "\n## other\n")
    second = compile_foreach_file(path)
    assert not second.has_errors(), second.diagnostics
    assert second.render_plans == first.render_plans
    assert second.workflow_signature != first.workflow_signature


def test_separate_producers_have_independent_constraints(tmp_path: Path) -> None:
    path = copy_foreach_fixture(tmp_path)
    payload, body = workflow_document(path)
    payload["nodes"].extend(
        [
            {**payload["nodes"][1], "id": "other_planner"},
            {
                **payload["nodes"][2],
                "id": "other",
                "needs": ["other_planner"],
                "items": "{{other_planner.findings}}",
                "max_items": 2,
            },
        ]
    )
    write_workflow_document(
        path, payload, body + "\n## other_planner\nPlan other items.\n## other\n"
    )
    preview = compile_foreach_file(path)
    assert not preview.has_errors(), preview.diagnostics
    for render in preview.render_plans:
        if render.node_id in {"planner", "other_planner"}:
            limit = 4 if render.node_id == "planner" else 2
            for stream in render.streams:
                assert f"at most {limit} items." in stream.fragments[-1].text


@pytest.mark.parametrize("bound_input", [False, True])
def test_imports_and_bound_inputs_target_composed_producer(
    tmp_path: Path, bound_input: bool
) -> None:
    path = copy_foreach_fixture(tmp_path)
    binding = {}
    nodes = []
    root_body = ""
    if bound_input:
        payload, body = workflow_document(path)
        payload["inputs"] = {"collection": "planner"}
        payload["nodes"][2]["needs"] = ["planner", "request"]
        payload["nodes"][1] = {
            "id": "planner",
            "mode": "input",
            "source": "{{file:unused.json}}",
        }
        body = body[body.index("## chunks") :]
        write_workflow_document(path, payload, body)
        binding = {"collection": "producer"}
        nodes = [
            {
                "id": "producer",
                "mode": "parallel",
                "providers": ["alpha"],
                "findings": True,
            }
        ]
        root_body = "\n## producer\nSelect items.\n"
    root = tmp_path / "root.md"
    write_workflow_document(
        root,
        {
            "name": "Root",
            "nodes": nodes,
            "imports": [{"path": path.name, "as": "outer", "inputs": binding}],
        },
        root_body,
    )
    preview = compile_foreach_file(root)
    assert not preview.has_errors(), preview.diagnostics
    producer = "producer" if bound_input else "outer.planner"
    assert preview.foreach_templates[0].items == "{{" + producer + ".findings}}"
    constrained = {
        render.node_id
        for render in preview.render_plans
        if any(
            "Executor findings:" in (fragment.text or "")
            for stream in render.streams
            for fragment in stream.fragments
        )
    }
    assert constrained == {producer}


@pytest.mark.parametrize(
    "items", [[], [{"text": "{{planner.findings}}"}], "{{request.output}}"]
)
def test_literals_output_sources_and_other_references_add_no_instruction(
    tmp_path: Path, items: list[dict[str, str]] | str
) -> None:
    path = copy_foreach_fixture(tmp_path)
    payload, body = workflow_document(path)
    payload["nodes"][2].update(items=items, inputs={"request": "planner"})
    body += "\nAlso consider {{planner.findings}}.\n"
    write_workflow_document(path, payload, body)
    source = load_workflow_source_for_preflight(path, tmp_path)
    assert with_collection_constraints(source.workflow) is source.workflow
    preview = compile_foreach_file(path)
    assert not preview.has_errors(), preview.diagnostics
    renders = [*preview.render_plans, *preview.foreach_templates[0].render_plans]
    assert all(
        "Executor findings:" not in (fragment.text or "")
        for render in renders
        for stream in render.streams
        for fragment in stream.fragments
    )


def test_earlier_group_output_does_not_propagate_cap(tmp_path: Path) -> None:
    path = copy_foreach_fixture(tmp_path)
    before = compile_foreach_file(path)
    payload, body = workflow_document(path)
    payload["nodes"].append(
        {
            **payload["nodes"][2],
            "id": "later",
            "needs": ["chunks"],
            "items": "{{chunks.output}}",
            "max_items": 1,
        }
    )
    write_workflow_document(path, payload, body + "\n## later\n")
    after = compile_foreach_file(path)
    assert not after.has_errors(), after.diagnostics
    original = next(plan for plan in before.render_plans if plan.node_id == "planner")
    render = next(plan for plan in after.render_plans if plan.node_id == "planner")
    assert original.streams == render.streams
    assert original.template_signature == render.template_signature
    assert (
        before.foreach_templates[0].template_digest
        == after.foreach_templates[0].template_digest
    )


def compile_source(
    source: PreflightWorkflowSource, root: Path
) -> PreflightCompilationPreview:
    config = foreach_config()
    snapshot = build_runtime_config_snapshot(config, Console(), no_live=True).snapshot
    return compile_preflight_preview(
        source, config, snapshot, PreflightCompileOptions(root, root / ".crewplane")
    )


@pytest.mark.parametrize(
    "empty_role,other_role", [("executor", "reviewer"), ("reviewer", "executor")]
)
def test_generated_guidance_cannot_validate_empty_authored_role(
    tmp_path: Path, empty_role: str, other_role: str
) -> None:
    path = copy_foreach_fixture(tmp_path)
    payload, body = workflow_document(path)
    payload["nodes"][1].update(
        mode="sequential",
        providers=["alpha", {"provider": "alpha", "role": "reviewer"}],
    )
    write_workflow_document(path, payload, body)
    source = load_workflow_source_for_preflight(path, tmp_path)
    source.workflow.nodes[1].prompt_segments = [
        PromptSegment(role=other_role, content="Authored task.")
    ]
    preview = compile_source(source, tmp_path)
    assert preview.has_errors()
    assert f"rendered {empty_role} prompt cannot be empty" in str(preview.diagnostics)
    assert not preview.render_plans


def test_repeated_compilation_preserves_source_and_authored_records(
    tmp_path: Path,
) -> None:
    path = copy_foreach_fixture(tmp_path)
    source = load_workflow_source_for_preflight(path, tmp_path)
    before = source.workflow.model_copy(deep=True)
    projected = with_collection_constraints(source.workflow)
    for original, node in zip(source.workflow.nodes, projected.nodes, strict=True):
        if node.id == "planner":
            assert node is not original
            assert node.needs is original.needs
            assert node.prompt_segments is not original.prompt_segments
            assert all(
                left is right
                for left, right in zip(
                    node.prompt_segments[:-1], original.prompt_segments, strict=True
                )
            )
        else:
            assert node is original
    first = compile_source(source, tmp_path)
    second = compile_source(source, tmp_path)
    assert not first.has_errors(), first.diagnostics
    assert first == second
    assert source.workflow == before
    state = CompileState()
    options = PreflightCompileOptions(
        tmp_path, tmp_path / ".crewplane"
    ).with_source_metadata(source)
    authored = compile_render_plan(source.workflow.nodes[1], options, state)
    compiled = next(plan for plan in first.render_plans if plan.node_id == "planner")
    assert authored is not None
    for original, stream in zip(authored.streams, compiled.streams, strict=True):
        assert stream.fragments[:-1] == original.fragments
        assert stream.fragments[-1].text.count("Executor findings:") == 1
    assert compiled.source_span == authored.source_span
    assert [
        token for token in first.token_catalog if token.node_id == "planner"
    ] == state.token_catalog

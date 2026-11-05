from pathlib import Path

import pytest

from crewplane.core.memory.candidates import LessonProposal
from crewplane.core.preflight.foreach.expansion import instantiate_foreach
from crewplane.core.preflight.foreach.reconstruction import apply_expansion
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.preflight.source import load_workflow_source_for_preflight
from tests.helpers.foreach import (
    copy_foreach_fixture,
    workflow_document,
    write_workflow_document,
)
from tests.helpers.project_memory import (
    accepted_memory,
    compile_memory_workflow,
    memory_plan,
)


def test_foreach_freezes_recall_once_and_only_remaps_selection_ownership(
    tmp_path: Path,
) -> None:
    path = copy_foreach_fixture(tmp_path)
    authored, body = workflow_document(path)
    authored["memory"] = True
    write_workflow_document(path, authored, body)
    source = load_workflow_source_for_preflight(path, tmp_path)
    snapshot = accepted_memory(
        LessonProposal(title="Review chunks", body="Review the captured chunk bytes.")
    )
    preview = compile_memory_workflow(tmp_path, source, snapshot)
    assert not preview.has_errors(), preview.diagnostics
    plan = memory_plan(preview, tmp_path)
    template = plan.foreach_templates[0]
    assert template.memory_selections
    assert all(
        selection.node_id.startswith("chunks.template.")
        for selection in template.memory_selections
    )
    assert all(
        selection.append_after_fragment_count > 0
        for selection in template.memory_selections
    )
    assert all(
        "at most 4 items" not in str(selection.query.contributions).lower()
        for selection in preview.memory_selections
    )
    assert any(
        "runtime collection item" in exclusion.reason
        for selection in template.memory_selections
        for exclusion in selection.query.exclusions
    )

    batch = instantiate_foreach(
        plan, template, [{"keyword": "unseen-one"}, {"keyword": "unseen-two"}], {}
    )
    expanded = apply_expansion(plan, batch)
    for selection in batch.memory_selections:
        ordinal = 0 if ".i0." in selection.node_id else 1
        original_id = selection.node_id.replace(f".i{ordinal}.", ".template.", 1)
        original = next(
            item
            for item in template.memory_selections
            if item.node_id == original_id and item.role == selection.role
        )
        assert selection.model_copy(update={"node_id": original_id}) == original
        assert "unseen" not in str(selection.query.model_dump())
    assert len(expanded.memory_selections) == len(plan.memory_selections) + len(
        batch.memory_selections
    )
    assert (
        PreflightExecutionPlan.model_validate_json(expanded.model_dump_json())
        == expanded
    )

    damaged = expanded.model_dump(mode="json")
    generated = next(
        selection
        for selection in damaged["memory_selections"]
        if ".i0." in selection["node_id"]
    )
    generated["omissions"].append("tampered frozen inspection")
    with pytest.raises(ValueError, match="Generated memory selections"):
        PreflightExecutionPlan.model_validate(damaged)

    damaged = expanded.model_dump(mode="json")
    selected = next(
        selection
        for selection in damaged["memory_selections"]
        if ".i0." in selection["node_id"] and selection["rendered"]
    )
    render = next(
        render
        for render in damaged["render_plans"]
        if render["node_id"] == selected["node_id"]
    )
    stream = next(
        stream
        for stream in render["streams"]
        if stream["target_role"] == selected["role"]
    )
    stream["fragments"].append(
        {
            **stream["fragments"][-1],
            "fragment_index": len(stream["fragments"]),
        }
    )
    with pytest.raises(ValueError, match="[Mm]emory.*append boundary"):
        PreflightExecutionPlan.model_validate(damaged)

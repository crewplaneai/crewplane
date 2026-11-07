from pathlib import Path

import pytest
from pydantic import ValidationError

from crewplane.core.memory.candidates import LessonProposal
from crewplane.core.memory.models import MemorySnapshot
from crewplane.core.preflight.models import PreflightCompilationPreview
from crewplane.core.preflight.plan_contract_memory import validate_memory_contract
from tests.helpers.project_memory import (
    accepted_memory,
    compile_memory_workflow,
    memory_workflow,
)


@pytest.fixture
def recall_preview(tmp_path: Path) -> PreflightCompilationPreview:
    preview = compile_memory_workflow(
        tmp_path,
        memory_workflow("protected reads", "protected reads"),
        accepted_memory(
            LessonProposal(title="Protected reads", body="Use verified bytes.")
        ),
    )
    assert not preview.has_errors(), preview.diagnostics
    assert all(selection.rendered for selection in preview.memory_selections)
    return preview


def test_role_coverage_precedes_selection_revalidation(
    recall_preview: PreflightCompilationPreview,
) -> None:
    invalid = recall_preview.memory_selections[0].model_copy(update={"max_records": 0})

    with pytest.raises(ValueError) as error:
        validate_memory_contract(recall_preview.nodes, [], [invalid])

    assert str(error.value) == (
        "Memory selections must match every enabled scheduled role exactly once."
    )


def test_selection_revalidation_precedes_stream_ownership(
    recall_preview: PreflightCompilationPreview,
) -> None:
    selections = recall_preview.memory_selections.copy()
    selections[0] = selections[0].model_copy(update={"max_records": 0})

    with pytest.raises(ValidationError) as error:
        validate_memory_contract(recall_preview.nodes, [], selections)

    assert error.value.errors()[0]["loc"] == ("max_records",)


def test_stream_ownership_precedes_later_selection_revalidation(
    recall_preview: PreflightCompilationPreview,
) -> None:
    selections = recall_preview.memory_selections.copy()
    selections[1] = selections[1].model_copy(update={"max_records": 0})

    with pytest.raises(ValueError) as error:
        validate_memory_contract(recall_preview.nodes, [], selections)

    assert str(error.value) == (
        "Memory selection requires exactly one owned role stream."
    )


def test_append_boundary_precedes_final_literal_and_later_selection_checks(
    recall_preview: PreflightCompilationPreview,
) -> None:
    selections = recall_preview.memory_selections.copy()
    selections[0] = selections[0].model_copy(
        update={
            "append_after_fragment_count": selections[0].append_after_fragment_count + 1
        }
    )
    selections[1] = selections[1].model_copy(update={"max_records": 0})
    recall_preview.render_plans[0].streams[0].fragments[-1].text = "tampered"

    with pytest.raises(ValueError) as error:
        validate_memory_contract(
            recall_preview.nodes, recall_preview.render_plans, selections
        )

    assert str(error.value) == (
        "Memory rendering disagrees with its frozen role-stream append boundary."
    )


@pytest.mark.parametrize("stream_count", [0, 2])
def test_memory_contract_requires_one_owned_role_stream(
    recall_preview: PreflightCompilationPreview, stream_count: int
) -> None:
    render = recall_preview.render_plans[0]
    executor, reviewer = render.streams
    render.streams = [executor] * stream_count + [reviewer]

    with pytest.raises(ValueError) as error:
        validate_memory_contract(
            recall_preview.nodes,
            recall_preview.render_plans,
            recall_preview.memory_selections,
        )

    assert str(error.value) == (
        "Memory selection requires exactly one owned role stream."
    )


@pytest.mark.parametrize("has_recall", [False, True])
def test_memory_contract_accepts_frozen_recall_without_mutating_inputs(
    tmp_path: Path, has_recall: bool
) -> None:
    snapshot = (
        accepted_memory(
            LessonProposal(title="Protected reads", body="Use verified bytes.")
        )
        if has_recall
        else MemorySnapshot()
    )
    preview = compile_memory_workflow(
        tmp_path, memory_workflow("protected reads", "protected reads"), snapshot
    )
    assert not preview.has_errors(), preview.diagnostics
    before = preview.model_dump(mode="json")

    result = validate_memory_contract(
        preview.nodes, preview.render_plans, preview.memory_selections
    )

    assert result is None
    assert preview.model_dump(mode="json") == before

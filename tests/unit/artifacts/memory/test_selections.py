import json
from pathlib import Path

import pytest

from crewplane.artifacts.manager import OutputManager
from crewplane.artifacts.memory.selections import (
    memory_selection_receipt,
    validate_historical_memory_selection,
    validate_memory_selection_receipt,
)
from crewplane.cli.run.preflight import materialize_preflight_success
from crewplane.core.memory.candidates import LessonProposal
from crewplane.core.memory.models import MemorySnapshot
from tests.helpers.platforms import symlink_or_skip
from tests.helpers.project_memory import (
    accepted_memory,
    compile_memory_workflow,
    memory_workflow,
)


@pytest.mark.parametrize("matched", [False, True])
def test_materialization_publishes_receipt_even_without_matching_memory(
    tmp_path: Path, matched: bool
) -> None:
    snapshot = (
        accepted_memory(
            LessonProposal(title="Protected reads", body="Use verified bytes.")
        )
        if matched
        else MemorySnapshot()
    )
    preview = compile_memory_workflow(
        tmp_path, memory_workflow("protected reads"), snapshot
    )
    output = OutputManager("memory", tmp_path / ".crewplane")
    plan = materialize_preflight_success(output, preview, tmp_path)
    receipt = output.stages_dir / "preflight/memory-selection.json"
    assert receipt.is_file()
    assert json.loads(receipt.read_text())["selections"][0]["selected"] == [
        item.model_dump(mode="json") for item in preview.memory_selections[0].selected
    ]
    validate_memory_selection_receipt(plan, output.stages_dir)
    validate_historical_memory_selection(plan, output.stages_dir)


@pytest.mark.parametrize(
    "damage", ["missing", "signature", "selection", "extra", "symlink", "plan"]
)
def test_receipt_and_historical_plan_disagreement_is_explicit(
    tmp_path: Path, damage: str
) -> None:
    preview = compile_memory_workflow(
        tmp_path,
        memory_workflow("protected reads"),
        accepted_memory(
            LessonProposal(title="Protected reads", body="Use verified bytes.")
        ),
    )
    output = OutputManager("memory", tmp_path / ".crewplane")
    plan = materialize_preflight_success(output, preview, tmp_path)
    receipt = output.stages_dir / "preflight/memory-selection.json"
    payload = json.loads(receipt.read_text())
    if damage == "missing":
        receipt.unlink()
    elif damage == "symlink":
        outside = tmp_path / "receipt.json"
        outside.write_bytes(receipt.read_bytes())
        receipt.unlink()
        symlink_or_skip(receipt, outside)
    elif damage == "plan":
        path = output.stages_dir / "preflight/execution-plan.json"
        data = json.loads(path.read_text())
        data["memory_selections"][0]["omissions"].append("altered")
        path.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    else:
        if damage == "signature":
            payload["workflow_signature"] = "different"
        elif damage == "selection":
            payload["selections"] = []
        else:
            payload["extra"] = "unsupported"
        receipt.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    with pytest.raises((OSError, ValueError)):
        validate_historical_memory_selection(plan, output.stages_dir)


def test_new_inspection_dates_do_not_prevent_valid_historical_reuse(
    tmp_path: Path,
) -> None:
    snapshot = accepted_memory(
        LessonProposal(title="Protected reads", body="Use verified bytes.")
    )
    first = compile_memory_workflow(
        tmp_path, memory_workflow("protected reads"), snapshot
    )
    output = OutputManager("memory", tmp_path / ".crewplane")
    plan = materialize_preflight_success(output, first, tmp_path)
    record = snapshot.records[0]
    revision = record.revisions[0]
    changed = snapshot.model_copy(
        update={
            "records": (
                record.model_copy(
                    update={
                        "revisions": (
                            revision.model_copy(
                                update={"created_at": "2027-01-01T00:00:00Z"}
                            ),
                        )
                    }
                ),
            )
        }
    )
    current = compile_memory_workflow(
        tmp_path, memory_workflow("protected reads"), changed
    )
    current_plan = plan.model_copy(
        update={"memory_selections": current.memory_selections}
    )
    assert current_plan.memory_selections != plan.memory_selections
    validate_historical_memory_selection(current_plan, output.stages_dir)


@pytest.mark.parametrize("damage", ["duplicate_append", "empty_retained_append"])
def test_historical_reuse_rejects_recall_outside_the_frozen_append_boundary(
    tmp_path: Path, damage: str
) -> None:
    preview = compile_memory_workflow(
        tmp_path,
        memory_workflow("protected reads"),
        accepted_memory(
            LessonProposal(title="Protected reads", body="Use verified bytes.")
        ),
    )
    output = OutputManager("memory", tmp_path / ".crewplane")
    plan = materialize_preflight_success(output, preview, tmp_path)
    path = output.stages_dir / "preflight/execution-plan.json"
    payload = json.loads(path.read_text())
    fragments = payload["render_plans"][0]["streams"][0]["fragments"]
    if damage == "duplicate_append":
        fragments.append({**fragments[-1], "fragment_index": len(fragments)})
    else:
        selection = plan.memory_selections[0].model_copy(
            update={"selected": (), "rendered": "", "rendered_chars": 0}
        )
        plan = plan.model_copy(update={"memory_selections": [selection]})
        payload["memory_selections"] = [selection.model_dump(mode="json")]
        receipt = output.stages_dir / "preflight/memory-selection.json"
        receipt.write_text(
            json.dumps(memory_selection_receipt(plan)),
            encoding="utf-8",
            newline="\n",
        )
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="[Mm]emory"):
        validate_historical_memory_selection(plan, output.stages_dir)

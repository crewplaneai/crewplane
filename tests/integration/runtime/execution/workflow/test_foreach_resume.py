import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.artifacts.expansions import load_expansion, reconstruct_expansions
from crewplane.artifacts.naming import node_state_relative_path
from crewplane.artifacts.resume.expansions import hydrate_expansions
from crewplane.artifacts.resume.hydration import hydrate_resume_frontier
from crewplane.artifacts.resume.validation import validate_resume_frontier
from crewplane.artifacts.run_history import RunHistoryRecord
from crewplane.core.preflight.foreach.expansion import instantiate_foreach
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.preflight.serialization import canonical_json_bytes
from crewplane.runtime.execution import execute_workflow
from tests.helpers.foreach import (
    compile_foreach_file,
    copy_foreach_fixture,
    freeze_foreach,
    workflow_document,
    write_workflow_document,
)
from tests.helpers.resume import make_run_manifest
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
)


def completed_run(
    root: Path,
    literal: bool = False,
) -> tuple[PreflightExecutionPlan, OutputManager, RunHistoryRecord]:
    path = copy_foreach_fixture(root)
    if literal:
        payload, body = workflow_document(path)
        payload["nodes"][2]["items"] = [{"chunk": 0}, {"chunk": 1}]
        del payload["nodes"][2]["max_items"]
        write_workflow_document(path, payload, body)
    preview = compile_foreach_file(path)
    output = OutputManager("foreach", base_dir=root)
    plan = freeze_foreach(preview, output)
    invoker = MockAgentInvoker(
        outputs=[
            '<!-- findings -->[{"chunk":0},{"chunk":1}]<!-- /findings -->',
            "<!-- findings -->zero<!-- /findings -->",
            "<!-- findings -->one<!-- /findings -->",
            "summary",
        ]
    )
    asyncio.run(
        execute_workflow(
            plan, output, invoker, preview.secret_context, workflow_identity="parent.md"
        )
    )
    manifest = make_run_manifest(
        output.run_id,
        output.run_key_name,
        status="succeeded",
        workflow_identity="parent.md",
        workflow_name=plan.workflow_name,
        workflow_signature=plan.workflow_signature,
    ).model_copy(
        update={
            "effective_runtime_config_signature": plan.effective_runtime_config_signature,
            "runtime_config_snapshot": plan.runtime_config_snapshot,
        }
    )
    manifest_path = output.write_run_manifest(manifest)
    return (
        plan,
        output,
        RunHistoryRecord(
            manifest, manifest_path, output.stages_dir, output.results_dir
        ),
    )


@pytest.mark.parametrize(
    "damage",
    ["record", "item", "child", "provenance", "source", "changed_source", "join"],
)
def test_resume_invalidates_only_affected_expansion_evidence(
    tmp_path: Path, damage: str
) -> None:
    plan, output, history = completed_run(tmp_path)
    bundle = output.stages_dir / "preflight/expansions/chunks"
    if damage == "record":
        (bundle / "expansion.json").unlink()
    elif damage == "item":
        (bundle / "items/0.json").write_bytes(b"{}")
    elif damage == "child":
        (output.results_dir / "chunks.i0.part.analyze-result.md").write_bytes(
            b"changed"
        )
    elif damage == "provenance":
        path = output.stages_dir / node_state_relative_path("chunks.i0.part.analyze")
        state = json.loads(path.read_bytes())
        state["expansion"]["digest"] = "f" * 64
        path.write_text(json.dumps(state), encoding="utf-8", newline="\n")
    elif damage in {"source", "changed_source"}:
        path = output.results_dir / "planner-findings.md"
        data = b'[{"chunk":7}]'
        path.write_bytes(data)
        if damage == "changed_source":
            state_path = output.stages_dir / node_state_relative_path("planner")
            state = json.loads(state_path.read_bytes())
            for descriptor in state["artifacts"]:
                if descriptor["kind"] == "findings":
                    descriptor.update(
                        size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest()
                    )
            state_path.write_text(json.dumps(state), encoding="utf-8", newline="\n")
    else:
        (output.results_dir / "chunks-result.md").unlink()
    frontier = validate_resume_frontier(history, plan)
    ids = set(frontier.resumed_node_ids)
    assert "request" in ids and "chunks" not in ids and "summarize" not in ids
    if damage in {"child", "provenance", "join"}:
        assert "chunks.i1.part.analyze" in ids
        assert set(frontier.expansion_batches) == {"chunks"}
        assert ("chunks.i0.part.analyze" in ids) == (damage == "join")
    else:
        assert not frontier.expansion_batches
        assert not any(node.startswith("chunks.") for node in ids)
        assert ("planner" in ids) == (damage != "source")
    if damage in {"source", "changed_source"}:
        # Cleanup still has the recorded graph when live producer evidence changes.
        reconstructed = reconstruct_expansions(plan, output.stages_dir)
        assert not reconstructed.invalid_groups
        assert len(reconstructed.plan.nodes) == 8


@pytest.mark.parametrize("literal", [False, True])
@pytest.mark.parametrize("damage", ["changed", "missing"])
def test_selected_expansion_is_rechecked_and_hydrated_before_successes(
    tmp_path: Path,
    literal: bool,
    damage: str,
) -> None:
    plan, output, history = completed_run(tmp_path, literal)
    (output.results_dir / "chunks-result.md").unlink()
    frontier = validate_resume_frontier(history, plan)
    target = OutputManager("foreach", base_dir=tmp_path)
    fresh = plan.model_copy(
        update={
            "run_id": target.run_id,
            "run_key_name": target.run_key_name,
            "context_root": str(target.stages_dir),
            "manifest_root": str(target.stages_dir / "manifests"),
        }
    )
    restored = hydrate_expansions(frontier, fresh, target)
    target.write_run_manifest(
        history.manifest.model_copy(
            update={
                "run_id": target.run_id,
                "run_key_name": target.run_key_name,
                "status": "running",
                "completed_at": None,
            }
        )
    )
    hydrate_resume_frontier(frontier, restored, target)
    evidence = restored.expansions["chunks"]
    assert (
        target.stages_dir / evidence.items[0].relative_path
    ).read_bytes() == b'{"chunk":0}'
    assert (target.results_dir / "chunks.i1.part.analyze-result.md").read_bytes() == (
        output.results_dir / "chunks.i1.part.analyze-result.md"
    ).read_bytes()
    state = json.loads(
        (
            target.stages_dir / node_state_relative_path("chunks.i0.part.analyze")
        ).read_bytes()
    )
    assert state["run_id"] == target.run_id
    assert state["expansion"]["digest"] == evidence.digest
    path = output.stages_dir / evidence.items[0].relative_path
    if damage == "changed":
        path.write_bytes(b"changed")
    else:
        path.unlink()
    error = ValueError if damage == "changed" else FileNotFoundError
    message = "descriptor" if damage == "changed" else "0.json"
    with pytest.raises(error, match=message):
        hydrate_expansions(frontier, fresh, OutputManager("foreach", base_dir=tmp_path))


def test_recomputed_evidence_cannot_change_literal_items(tmp_path: Path) -> None:
    plan, output, history = completed_run(tmp_path, literal=True)
    template = plan.foreach_templates[0]
    batch, items = load_expansion(plan, template, output.stages_dir)
    items.reverse()
    forged = instantiate_foreach(plan, template, items, batch.state.caller_artifacts)
    (output.stages_dir / forged.state.collection.relative_path).write_bytes(
        canonical_json_bytes(items)
    )
    for evidence, item in zip(forged.state.items, items, strict=True):
        (output.stages_dir / evidence.relative_path).write_bytes(
            canonical_json_bytes(item)
        )
    output.write_preflight_json("expansions/chunks/expansion.json", forged)
    with pytest.raises(ValueError, match="Literal expansion items differ"):
        load_expansion(plan, template, output.stages_dir)
    assert reconstruct_expansions(plan, output.stages_dir).invalid_groups == {"chunks"}
    assert not validate_resume_frontier(history, plan).expansion_batches


def test_forged_additions_and_uncommitted_bundles_do_not_authorize_reuse(
    tmp_path: Path,
) -> None:
    plan, output, history = completed_run(tmp_path)
    record = output.stages_dir / "preflight/expansions/chunks/expansion.json"
    payload = json.loads(record.read_bytes())
    payload["nodes"][1]["provider_records"][0]["model"] = "forged"
    record.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    reconstruction = reconstruct_expansions(plan, output.stages_dir)
    assert reconstruction.invalid_groups == {"chunks"}
    assert reconstruction.plan == plan
    assert set(validate_resume_frontier(history, plan).resumed_node_ids) == {
        "request",
        "planner",
    }

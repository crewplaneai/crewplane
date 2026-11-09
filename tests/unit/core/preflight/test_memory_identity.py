from pathlib import Path

import pytest

from crewplane.core.memory.candidates import CandidateDocument, LessonProposal
from crewplane.core.memory.lifecycle import (
    Decision,
    accept_batch,
    forget_record,
    import_candidates,
)
from crewplane.core.memory.models import ProposalProvenance
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.version import SCHEMA_VERSION
from tests.helpers.project_memory import (
    accepted_memory,
    compile_memory_workflow,
    memory_config,
    memory_plan,
    memory_workflow,
)


def test_selected_correction_and_forget_change_identity_but_pending_changes_do_not(
    tmp_path: Path,
) -> None:
    memory = accepted_memory(
        LessonProposal(title="Protected reads", body="Keep the initial check.")
    )
    initial = compile_memory_workflow(
        tmp_path, memory_workflow("protected reads"), memory
    )
    record = memory.records[0]
    proposals = CandidateDocument(
        schema_version=SCHEMA_VERSION,
        lessons=(
            LessonProposal(
                title="Protected reads",
                body="Keep the stronger check.",
                corrects_record=record.record_id,
                expected_accepted_revision=record.accepted_revision,
            ),
            LessonProposal(title="Unrelated", body="different subject"),
        ),
    )
    pending, identities = import_candidates(
        memory,
        proposals,
        ProposalProvenance(author="human", source_identity="revision"),
        {},
    )
    assert (
        compile_memory_workflow(
            tmp_path, memory_workflow("protected reads"), pending
        ).workflow_signature
        == initial.workflow_signature
    )
    correction = identities[0]
    changed = accept_batch(
        pending,
        (
            Decision(
                correction.record_id,
                correction.revision_id,
                correction.candidate_digest,
                correction.expected_accepted_revision,
                "accept",
            ),
        ),
    )
    assert (
        compile_memory_workflow(
            tmp_path, memory_workflow("protected reads"), changed
        ).workflow_signature
        != initial.workflow_signature
    )
    forgotten = forget_record(memory, record.record_id, record.state_digest)
    assert (
        compile_memory_workflow(
            tmp_path, memory_workflow("protected reads"), forgotten
        ).workflow_signature
        != initial.workflow_signature
    )
    assert (
        memory_plan(initial, tmp_path).render_plans[0].streams[0].fragments[-1].text
        == initial.memory_selections[0].rendered
    )


def test_learning_policy_and_observation_dates_do_not_change_parent_identity(
    tmp_path: Path,
) -> None:
    memory = accepted_memory(
        LessonProposal(title="Protected reads", body="Use verified bytes.")
    )
    config = memory_config()
    first = compile_memory_workflow(
        tmp_path, memory_workflow("protected reads"), memory, config
    )
    record = memory.records[0]
    revision = record.revisions[0]
    dated = revision.model_copy(
        update={
            "created_at": "2027-01-01T00:00:00Z",
            "acceptance": revision.acceptance.model_copy(
                update={"accepted_at": "2027-01-01T00:00:00Z"}
            ),
        }
    )
    changed = memory.model_copy(
        update={
            "transaction_id": "other",
            "records": (record.model_copy(update={"revisions": (dated,)}),),
        }
    )
    config.settings.memory.learn = True
    config.settings.memory.extractor = "beta"
    config.settings.memory.extraction_timeout_seconds = 333
    second = compile_memory_workflow(
        tmp_path, memory_workflow("protected reads"), changed, config
    )
    assert first.memory_selections != second.memory_selections
    assert first.workflow_signature == second.workflow_signature
    assert first.memory_selections[0].rendered == second.memory_selections[0].rendered


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "duplicate",
        "owner",
        "role",
        "optout",
        "text",
        "fragment_role",
        "fragment_index",
        "duplicate_append",
        "empty_retained_append",
        "append_boundary",
        "missing_append_boundary",
        "identity",
        "limit",
        "rendered_size",
        "query",
    ],
)
def test_plan_rejects_tampered_frozen_selection_contract(
    tmp_path: Path, damage: str
) -> None:
    preview = compile_memory_workflow(
        tmp_path,
        memory_workflow("protected reads"),
        accepted_memory(
            LessonProposal(title="Protected reads", body="Use verified bytes.")
        ),
    )
    payload = memory_plan(preview, tmp_path).model_dump(mode="json")
    selection = payload["memory_selections"][0]
    fragment = payload["render_plans"][0]["streams"][0]["fragments"][-1]
    if damage == "missing":
        payload["memory_selections"] = []
    elif damage == "duplicate":
        payload["memory_selections"].append(selection)
    elif damage == "owner":
        selection["node_id"] = "missing"
    elif damage == "role":
        selection["role"] = "reviewer"
    elif damage == "optout":
        payload["nodes"][0]["memory"] = False
    elif damage == "text":
        fragment["text"] += "changed"
    elif damage == "fragment_role":
        fragment["source_role"] = "executor"
    elif damage == "fragment_index":
        fragment["fragment_index"] = 17
    elif damage == "duplicate_append":
        fragments = payload["render_plans"][0]["streams"][0]["fragments"]
        fragments.append({**fragment, "fragment_index": len(fragments)})
    elif damage == "empty_retained_append":
        selection.update(selected=[], rendered="", rendered_chars=0)
    elif damage == "append_boundary":
        selection["append_after_fragment_count"] += 1
    elif damage == "missing_append_boundary":
        del selection["append_after_fragment_count"]
    elif damage == "identity":
        selection["selected"][0]["revision"]["content_digest"] = "f" * 64
    elif damage == "limit":
        selection["max_chars"] = 1
    elif damage == "rendered_size":
        selection["rendered_chars"] = 0
    elif damage == "query":
        selection["query"]["terms"].append("fabricated")
    with pytest.raises(ValueError, match="[Mm]emory|[Ss]election|[Qq]uery"):
        PreflightExecutionPlan.model_validate(payload)


@pytest.mark.parametrize("setting,value", [("max_records", 1), ("max_chars", 1)])
def test_effective_recall_limits_change_identity_and_no_fit_remains_recorded(
    tmp_path: Path, setting: str, value: int
) -> None:
    config = memory_config()
    snapshot = accepted_memory(
        LessonProposal(title="Protected reads", body="Use verified bytes.")
    )
    initial = compile_memory_workflow(
        tmp_path, memory_workflow("protected reads"), snapshot, config
    )
    setattr(config.settings.memory, setting, value)
    changed = compile_memory_workflow(
        tmp_path, memory_workflow("protected reads"), snapshot, config
    )
    assert changed.workflow_signature != initial.workflow_signature
    if setting == "max_chars":
        assert not changed.memory_selections[0].rendered
        assert any(
            "does not fit" in message
            for message in changed.memory_selections[0].omissions
        )


def test_unselected_acceptance_rejection_and_explanation_changes_are_unsigned(
    tmp_path: Path,
) -> None:
    memory = accepted_memory(
        LessonProposal(title="Protected reads", body="Use verified bytes.")
    )
    task = memory_workflow("protected reads")
    initial = compile_memory_workflow(tmp_path, task, memory)
    pending, identities = import_candidates(
        memory,
        CandidateDocument(
            schema_version=SCHEMA_VERSION,
            lessons=(LessonProposal(title="Quantum optics", body="photon"),),
        ),
        ProposalProvenance(author="human", source_identity="other"),
        {},
    )
    candidate = identities[0]
    for decision in ("accept", "reject"):
        changed = accept_batch(
            pending,
            (
                Decision(
                    candidate.record_id,
                    candidate.revision_id,
                    candidate.candidate_digest,
                    candidate.expected_accepted_revision,
                    decision,
                ),
            ),
        )
        preview = compile_memory_workflow(tmp_path, task, changed)
        assert preview.workflow_signature == initial.workflow_signature
    selection = initial.memory_selections[0]
    changed_selection = selection.model_copy(
        update={
            "omissions": ("inspection only",),
            "query": selection.query.model_copy(update={"exclusions": ()}),
        }
    )
    assert selection.semantic_payload() == changed_selection.semantic_payload()


def test_extractor_adapter_registration_remains_part_of_parent_identity(
    tmp_path: Path,
) -> None:
    config = memory_config()
    snapshot = accepted_memory(
        LessonProposal(title="Protected reads", body="Use verified bytes.")
    )
    task = memory_workflow("protected reads")
    initial = compile_memory_workflow(tmp_path, task, snapshot, config)
    config.settings.integrations.invoker.options["observation_delay_seconds"] = 0.1
    changed = compile_memory_workflow(tmp_path, task, snapshot, config)
    assert changed.workflow_signature != initial.workflow_signature


def test_equivalent_effective_memory_defaults_have_the_same_identity(
    tmp_path: Path,
) -> None:
    snapshot = accepted_memory(
        LessonProposal(title="Protected reads", body="Use verified bytes.")
    )
    inherited = memory_workflow("protected reads")
    explicit = inherited.model_copy(deep=True)
    explicit.memory = False
    explicit.nodes[0].memory = True
    first = compile_memory_workflow(tmp_path, inherited, snapshot)
    second = compile_memory_workflow(tmp_path, explicit, snapshot)
    assert first.workflow_signature == second.workflow_signature
    inherited.memory = False
    explicit.nodes[0].memory = False
    first_disabled = compile_memory_workflow(tmp_path, inherited, None)
    second_disabled = compile_memory_workflow(tmp_path, explicit, None)
    assert first_disabled.workflow_signature == second_disabled.workflow_signature
    assert first_disabled.memory_selections == second_disabled.memory_selections == []

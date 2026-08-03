from __future__ import annotations

import pytest

from crewplane.artifacts.workspace.state.validation import (
    provider_rendered_workspace_files_match,
)
from tests.helpers.resume import make_plan, make_workspace_file_locator


def test_rendered_workspace_file_match_rejects_bool_descriptor_round_num() -> None:
    locator = make_workspace_file_locator().model_copy(
        update={"target": "executor_prompt"}
    )
    plan = make_plan().model_copy(update={"workspace_file_locators": [locator]})
    payload = {
        "node_id": "a",
        "task_id": "alpha",
        "role": "executor",
        "round_num": 1,
        "audit_round_num": None,
        "invocation_source": {
            "source_kind": "project",
            "source_node_id": None,
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "candidate_sequence": None,
        },
        "rendered_workspace_files": [
            {
                "occurrence_id": locator.occurrence_id,
                "invocation_id": "a.executor.alpha.round-1",
                "role": "executor",
                "round_num": True,
                "audit_round_num": None,
                "source_kind": "project",
                "source_node_id": None,
                "source_commit": "a" * 40,
                "source_tree": "b" * 40,
                "candidate_sequence": None,
                "workspace_relative_path": locator.workspace_relative_path,
                "target": "executor_prompt",
                "byte_source": "git_blob",
                "literal_path_verified": True,
                "utf8_validated": True,
                "git_blob": locator.git_blob,
                "git_file_mode": locator.git_file_mode,
                "byte_size": locator.byte_size,
                "canonical_blob_sha256": locator.canonical_blob_sha256,
                "injected_sha256": locator.canonical_blob_sha256,
            }
        ],
    }

    assert not provider_rendered_workspace_files_match(plan, plan.nodes[0], payload)


def test_rendered_workspace_file_match_rejects_non_string_occurrence_id() -> None:
    locator = make_workspace_file_locator().model_copy(
        update={"target": "executor_prompt"}
    )
    plan = make_plan().model_copy(update={"workspace_file_locators": [locator]})
    payload = {
        "node_id": "a",
        "task_id": "alpha",
        "role": "executor",
        "round_num": 1,
        "audit_round_num": None,
        "invocation_source": {
            "source_kind": "project",
            "source_node_id": None,
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "candidate_sequence": None,
        },
        "rendered_workspace_files": [
            {
                "occurrence_id": ["not", "hashable"],
                "invocation_id": "a.executor.alpha.round-1",
                "role": "executor",
                "round_num": 1,
                "audit_round_num": None,
                "source_kind": "project",
                "source_node_id": None,
                "source_commit": "a" * 40,
                "source_tree": "b" * 40,
                "candidate_sequence": None,
                "workspace_relative_path": locator.workspace_relative_path,
                "target": "executor_prompt",
                "byte_source": "git_blob",
                "literal_path_verified": True,
                "utf8_validated": True,
                "git_blob": locator.git_blob,
                "git_file_mode": locator.git_file_mode,
                "byte_size": locator.byte_size,
                "canonical_blob_sha256": locator.canonical_blob_sha256,
                "injected_sha256": locator.canonical_blob_sha256,
            }
        ],
    }

    assert not provider_rendered_workspace_files_match(plan, plan.nodes[0], payload)


@pytest.mark.parametrize(
    "rendered",
    [
        pytest.param(None, id="absent"),
        pytest.param([], id="empty"),
    ],
)
def test_rendered_workspace_files_allow_empty_metadata_without_expected_locator(
    rendered: object,
) -> None:
    plan = make_plan()
    payload = {"role": "unknown", "rendered_workspace_files": rendered}

    assert provider_rendered_workspace_files_match(plan, plan.nodes[0], payload)


@pytest.mark.parametrize(
    "rendered",
    [
        pytest.param(None, id="missing"),
        pytest.param({}, id="mapping"),
        pytest.param([], id="wrong-count"),
        pytest.param([None], id="non-mapping-descriptor"),
    ],
)
def test_rendered_workspace_files_require_one_mapping_per_expected_locator(
    rendered: object,
) -> None:
    plan, payload, _descriptor = _rendered_case()
    payload["rendered_workspace_files"] = rendered

    assert not provider_rendered_workspace_files_match(plan, plan.nodes[0], payload)


def test_rendered_workspace_files_reject_duplicate_occurrences() -> None:
    locator = make_workspace_file_locator().model_copy(
        update={"target": "executor_prompt"}
    )
    second_locator = locator.model_copy(update={"locator_id": "second"})
    plan, payload, descriptor = _rendered_case(locators=[locator, second_locator])
    payload["rendered_workspace_files"] = [descriptor, descriptor.copy()]

    assert not provider_rendered_workspace_files_match(plan, plan.nodes[0], payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("occurrence_id", "other", id="occurrence"),
        pytest.param("invocation_id", "other", id="invocation"),
        pytest.param("role", "reviewer", id="role"),
        pytest.param("round_num", 2, id="round"),
        pytest.param("audit_round_num", 1, id="audit-round"),
        pytest.param("source_kind", "node", id="source-kind"),
        pytest.param("source_node_id", "upstream", id="source-node"),
        pytest.param("source_commit", "c" * 40, id="source-commit"),
        pytest.param("source_tree", "d" * 40, id="source-tree"),
        pytest.param("candidate_sequence", 1, id="candidate-sequence"),
        pytest.param("workspace_relative_path", "other.txt", id="workspace-path"),
        pytest.param("target", "reviewer_prompt", id="target"),
        pytest.param("byte_source", "workspace", id="byte-source"),
        pytest.param("literal_path_verified", False, id="literal-path"),
        pytest.param("utf8_validated", False, id="utf8"),
    ],
)
def test_rendered_workspace_file_rejects_identity_mismatch(
    field: str,
    value: object,
) -> None:
    plan, payload, descriptor = _rendered_case()
    descriptor[field] = value

    assert not provider_rendered_workspace_files_match(plan, plan.nodes[0], payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("node_id", None, id="node-id"),
        pytest.param("task_id", None, id="task-id"),
        pytest.param("role", None, id="role"),
        pytest.param("round_num", True, id="boolean-round"),
        pytest.param("round_num", "1", id="string-round"),
        pytest.param("audit_round_num", True, id="boolean-audit-round"),
        pytest.param("audit_round_num", "1", id="string-audit-round"),
    ],
)
def test_rendered_workspace_file_rejects_invalid_invocation_fields(
    field: str,
    value: object,
) -> None:
    plan, payload, _descriptor = _rendered_case()
    payload[field] = value

    assert not provider_rendered_workspace_files_match(plan, plan.nodes[0], payload)


def test_rendered_workspace_file_accepts_audit_round_invocation_id() -> None:
    plan, payload, descriptor = _rendered_case()
    payload["audit_round_num"] = 2
    descriptor["audit_round_num"] = 2
    descriptor["invocation_id"] = "a.executor.alpha.audit-2.round-1"

    assert provider_rendered_workspace_files_match(plan, plan.nodes[0], payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("byte_size", True, id="boolean-size"),
        pytest.param("byte_size", -1, id="negative-size"),
        pytest.param("git_blob", "not-hex", id="blob"),
        pytest.param("git_file_mode", "120000", id="mode"),
        pytest.param("injected_sha256", "short", id="injected-digest"),
        pytest.param("canonical_blob_sha256", "short", id="canonical-digest"),
        pytest.param("canonical_blob_sha256", "f" * 64, id="canonical-mismatch"),
    ],
)
def test_rendered_workspace_file_rejects_invalid_blob_metadata(
    field: str,
    value: object,
) -> None:
    plan, payload, descriptor = _rendered_case()
    descriptor[field] = value

    assert not provider_rendered_workspace_files_match(plan, plan.nodes[0], payload)


def test_rendered_workspace_file_accepts_reviewer_locator() -> None:
    locator = make_workspace_file_locator().model_copy(
        update={"target": "reviewer_prompt"}
    )
    plan, payload, descriptor = _rendered_case(locators=[locator])
    payload["role"] = "reviewer"
    descriptor["role"] = "reviewer"
    descriptor["target"] = "reviewer_prompt"
    descriptor["invocation_id"] = "a.reviewer.alpha.round-1"

    assert provider_rendered_workspace_files_match(plan, plan.nodes[0], payload)


def _rendered_case(
    locators: list[object] | None = None,
) -> tuple[object, dict[str, object], dict[str, object]]:
    locator = make_workspace_file_locator().model_copy(
        update={"target": "executor_prompt"}
    )
    selected_locators = [locator] if locators is None else locators
    plan = make_plan().model_copy(update={"workspace_file_locators": selected_locators})
    payload: dict[str, object] = {
        "node_id": "a",
        "task_id": "alpha",
        "role": "executor",
        "round_num": 1,
        "audit_round_num": None,
        "invocation_source": {
            "source_kind": "project",
            "source_node_id": None,
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "candidate_sequence": None,
        },
    }
    descriptor: dict[str, object] = {
        "occurrence_id": locator.occurrence_id,
        "invocation_id": "a.executor.alpha.round-1",
        "role": "executor",
        "round_num": 1,
        "audit_round_num": None,
        "source_kind": "project",
        "source_node_id": None,
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "candidate_sequence": None,
        "workspace_relative_path": locator.workspace_relative_path,
        "target": "executor_prompt",
        "byte_source": "git_blob",
        "literal_path_verified": True,
        "utf8_validated": True,
        "git_blob": locator.git_blob,
        "git_file_mode": locator.git_file_mode,
        "byte_size": locator.byte_size,
        "canonical_blob_sha256": locator.canonical_blob_sha256,
        "injected_sha256": locator.canonical_blob_sha256,
    }
    payload["rendered_workspace_files"] = [descriptor]
    return plan, payload, descriptor

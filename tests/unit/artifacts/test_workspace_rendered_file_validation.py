from __future__ import annotations

from orchestrator_cli.artifacts.workspace.rendered_file_validation import (
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

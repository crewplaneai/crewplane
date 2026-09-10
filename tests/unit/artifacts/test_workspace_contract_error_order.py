from __future__ import annotations

from crewplane.artifacts.workspace.state.contracts import (
    workspace_state_contract_errors,
)
from tests.unit.artifacts.workspace_state_contracts_support import (
    OID_C,
    OID_D,
    valid_worktree_payload,
)


def test_workspace_contract_preserves_workspace_error_order() -> None:
    payload = valid_worktree_payload()
    workspace = payload["workspace"]
    assert isinstance(workspace, dict)
    workspace["materialization"] = "snapshot_checkout"
    workspace["writable"] = False
    workspace["retention"] = "not_applicable"
    payload["status"] = "failed"

    errors = workspace_state_contract_errors(payload, "resume")

    assert errors == (
        "worktree materialization mismatch",
        "managed workspace must be writable",
        "resume requires a succeeded workspace",
        "terminal workspace has invalid retention",
    )


def test_workspace_contract_preserves_source_error_order() -> None:
    payload = valid_worktree_payload()
    source = payload["source"]
    assert isinstance(source, dict)
    source.update(
        {
            "kind": "node",
            "node_id": "",
            "commit": "invalid",
            "tree": "invalid",
            "bundle_path": "",
            "bundle_sha256": "",
            "bundle_size_bytes": -1,
            "bundle_ref": "",
            "upstream_sources": [None],
        }
    )

    errors = workspace_state_contract_errors(payload, "resume")

    assert errors[:7] == (
        "node source requires a source node",
        "node source lacks bundle_path",
        "node source lacks bundle_sha256",
        "node source lacks bundle_size_bytes",
        "node source lacks bundle_ref",
        "node source upstream descriptor is invalid",
        "source commit or tree is invalid",
    )


def test_workspace_contract_preserves_lineage_result_error_order() -> None:
    payload = valid_worktree_payload()
    result = payload["result"]
    bundle = payload["bundle"]
    assert isinstance(result, dict)
    assert isinstance(bundle, dict)
    for field in (
        "candidate_commit",
        "result_commit",
        "candidate_tree",
        "result_tree",
    ):
        result[field] = None
    result["changed_path_count"] = -1
    bundle["verified"] = False

    errors = workspace_state_contract_errors(payload, "resume")

    assert errors[:6] == (
        "lineage result lacks candidate_commit",
        "lineage result lacks result_commit",
        "lineage result lacks candidate_tree",
        "lineage result lacks result_tree",
        "lineage result lacks changed_path_count",
        "lineage result lacks verified bundle evidence",
    )


def test_workspace_contract_preserves_ref_error_order() -> None:
    payload = valid_worktree_payload()
    publication = payload["ref_publication"]
    assert isinstance(publication, dict)
    publication.update(
        {
            "phase": "prepared",
            "repository_id": "other-repo",
            "run_id": "other-run",
            "node_id": "other-node",
            "task_id": "other-task",
            "role": "reviewer",
            "round_num": 2,
            "audit_round_num": 1,
        }
    )
    destinations = publication["destinations"]
    assert isinstance(destinations, dict)
    destinations["candidate"] = {
        "name": "refs/other-candidate",
        "target_oid": OID_D,
    }
    destinations["result"] = {
        "name": "refs/other-result",
        "target_oid": OID_C,
        "expected_old_oid": "invalid",
    }
    payload["temporary_refs"] = [{}, {}]

    errors = workspace_state_contract_errors(payload, "resume")

    assert errors == (
        "successful lineage has only prepared ref publication",
        "ref publication repository mismatch",
        "ref publication run identity mismatch",
        "ref publication node_id mismatch",
        "ref publication task_id mismatch",
        "ref publication role mismatch",
        "ref publication round_num mismatch",
        "ref publication audit_round_num mismatch",
        "ref publication candidate expected OID is invalid",
        "ref publication candidate name mismatch",
        "ref publication candidate escapes invocation scope",
        "ref publication candidate target mismatch",
        "ref publication result expected OID is invalid",
        "ref publication result name mismatch",
        "ref publication result escapes invocation scope",
        "ref publication result target mismatch",
        "temporary ref claim is contradictory",
        "temporary ref claim is contradictory",
    )


def test_workspace_contract_continues_after_malformed_destination() -> None:
    payload = valid_worktree_payload()
    publication = payload["ref_publication"]
    assert isinstance(publication, dict)
    destinations = publication["destinations"]
    assert isinstance(destinations, dict)
    destinations["candidate"] = {
        "name": "",
        "target_oid": OID_C,
        "expected_old_oid": "invalid",
    }
    destinations["result"] = {
        "name": "refs/other-result",
        "target_oid": OID_C,
        "expected_old_oid": "invalid",
    }

    errors = workspace_state_contract_errors(payload, "resume")

    assert errors == (
        "invalid ref publication destination",
        "ref publication result expected OID is invalid",
        "ref publication result name mismatch",
        "ref publication result escapes invocation scope",
        "ref publication result target mismatch",
    )


def test_workspace_contract_preserves_invalid_publication_early_return() -> None:
    payload = valid_worktree_payload()
    publication = payload["ref_publication"]
    assert isinstance(publication, dict)
    publication.update(
        {
            "phase": "unknown",
            "repository_id": "other-repo",
            "run_id": "other-run",
        }
    )
    payload["temporary_refs"] = [{}]

    errors = workspace_state_contract_errors(payload, "resume")

    assert errors == (
        "lineage result lacks ref publication phase",
        "temporary ref claim is contradictory",
    )

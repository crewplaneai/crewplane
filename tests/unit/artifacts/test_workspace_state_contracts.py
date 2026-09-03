from __future__ import annotations

from copy import deepcopy
from typing import Final

import pytest

from crewplane.artifacts.workspace.state.contracts import (
    PersistedWorkspaceOperation,
    require_workspace_state_contract,
    workspace_state_contract_errors,
    workspace_state_contract_is_valid,
)
from crewplane.version import SCHEMA_VERSION

OID_A: Final = "a" * 40
OID_B: Final = "b" * 40
OID_C: Final = "c" * 40
OID_D: Final = "d" * 40
SHA256: Final = "e" * 64
DELETE: Final = object()


@pytest.mark.parametrize(
    ("path", "value", "expected_error", "operation"),
    (
        (("role",), "observer", "invalid provider role", "resume"),
        (("workspace_kind",), "unknown", "invalid workspace kind", "resume"),
        (
            ("workspace", "materialization"),
            "snapshot_checkout",
            "worktree materialization mismatch",
            "resume",
        ),
        (
            ("workspace", "reuse_generation"),
            0,
            "materialized worktree lacks reuse generation",
            "resume",
        ),
        (
            ("workspace", "writable"),
            False,
            "managed workspace must be writable",
            "resume",
        ),
        (
            ("execution", "workspace_path"),
            None,
            "cleanup workspace lacks its physical path",
            "cleanup",
        ),
        (("status",), "failed", "resume requires a succeeded workspace", "resume"),
        (("status",), "planned", "cleanup requires a terminal outcome", "cleanup"),
        (
            ("workspace", "retention"),
            "not_applicable",
            "terminal workspace has invalid retention",
            "resume",
        ),
        (
            ("source", "node_id"),
            "upstream",
            "project source cannot name a source node",
            "resume",
        ),
        (
            ("source", "upstream_sources"),
            [{}],
            "project source cannot contain upstream sources",
            "resume",
        ),
        (
            ("source", "bundle_path"),
            "bundle",
            "project source cannot contain bundle evidence",
            "resume",
        ),
        (("source", "kind"), "other", "invalid source kind", "resume"),
        (
            ("source", "commit"),
            "invalid",
            "source commit or tree is invalid",
            "resume",
        ),
        (
            ("invocation_source", "source_kind"),
            "node",
            "invocation source source_kind mismatch",
            "resume",
        ),
        (
            ("process_drain", "status"),
            "unknown",
            "missing process drain evidence",
            "resume",
        ),
        (
            ("process_drain", "status"),
            "unresolved",
            "successful workspace has unresolved process liveness",
            "resume",
        ),
        (
            ("result", "candidate_commit"),
            None,
            "lineage result lacks candidate_commit",
            "resume",
        ),
        (
            ("result", "changed_path_count"),
            -1,
            "lineage result lacks changed_path_count",
            "resume",
        ),
        (
            ("bundle", "verified"),
            False,
            "lineage result lacks verified bundle evidence",
            "resume",
        ),
        (
            ("ref_publication", "phase"),
            "unknown",
            "lineage result lacks ref publication phase",
            "resume",
        ),
        (
            ("ref_publication", "phase"),
            "prepared",
            "successful lineage has only prepared ref publication",
            "resume",
        ),
        (
            ("ref_publication", "repository_id"),
            "other",
            "ref publication repository mismatch",
            "resume",
        ),
        (
            ("ref_publication", "run_id"),
            "other",
            "ref publication run identity mismatch",
            "resume",
        ),
        (
            ("ref_publication", "node_id"),
            "other",
            "ref publication node_id mismatch",
            "resume",
        ),
        (
            ("ref_publication", "destinations", "result"),
            DELETE,
            "ref publication destinations are incomplete",
            "resume",
        ),
        (
            ("ref_publication", "destinations", "candidate", "name"),
            "",
            "invalid ref publication destination",
            "resume",
        ),
        (
            ("ref_publication", "destinations", "candidate", "name"),
            "refs/other",
            "ref publication candidate name mismatch",
            "resume",
        ),
        (
            ("ref_publication", "destinations", "candidate", "target_oid"),
            OID_D,
            "ref publication candidate target mismatch",
            "resume",
        ),
        (
            (
                "ref_publication",
                "destinations",
                "candidate",
                "expected_old_oid",
            ),
            DELETE,
            "ref publication candidate expected OID is invalid",
            "resume",
        ),
        (
            ("temporary_refs",),
            {},
            "temporary ref evidence is invalid",
            "resume",
        ),
        (
            ("temporary_refs",),
            [{}],
            "temporary ref claim is contradictory",
            "resume",
        ),
    ),
)
def test_workspace_state_contract_rejects_contradictory_evidence(
    path: tuple[str, ...],
    value: object,
    expected_error: str,
    operation: PersistedWorkspaceOperation,
) -> None:
    payload = _valid_worktree_payload()
    _set_path(payload, path, value)

    errors = workspace_state_contract_errors(payload, operation)

    assert expected_error in errors


def test_workspace_state_contract_validates_snapshot_reporting_shape() -> None:
    payload = _valid_snapshot_payload()
    assert workspace_state_contract_is_valid(payload, "resume")

    workspace = payload["workspace"]
    assert isinstance(workspace, dict)
    workspace["materialization"] = "worktree_checkout"
    workspace["lineage_producer"] = True
    workspace["reuse_generation"] = 1
    payload["result"] = {
        "drift_scan_complete": False,
        "changed_path_count": 0,
    }

    errors = workspace_state_contract_errors(payload, "resume")

    assert "snapshot materialization mismatch" in errors
    assert "snapshot cannot produce lineage" in errors
    assert "snapshot cannot claim a reuse generation" in errors
    assert "incomplete snapshot drift contains exact claims" in errors


def test_cleanup_contract_rejects_unresolved_failed_process_liveness() -> None:
    payload = _valid_worktree_payload()
    payload["status"] = "failed"
    payload["process_drain"] = {"status": "unresolved", "pid": 42}

    errors = workspace_state_contract_errors(payload, "cleanup")

    assert "cleanup workspace has unresolved process liveness" in errors


@pytest.mark.parametrize(
    "operation",
    (
        "materialization",
        "resume",
        "duplicate_skip",
        "rendering",
        "export",
        "cleanup",
        "ref_cleanup",
    ),
)
def test_succeeded_workspace_contract_rejects_unresolved_workspace_mutator(
    operation: PersistedWorkspaceOperation,
) -> None:
    payload = _valid_worktree_payload()
    payload["workspace_mutator"] = {
        "status": "unresolved",
        "operation": "success_finalizer",
    }

    errors = workspace_state_contract_errors(payload, operation)

    assert "successful workspace has unresolved workspace mutator" in errors


@pytest.mark.parametrize("operation", ("cleanup", "ref_cleanup"))
def test_cleanup_contract_rejects_unresolved_failed_workspace_mutator(
    operation: PersistedWorkspaceOperation,
) -> None:
    payload = _valid_worktree_payload()
    payload["status"] = "failed"
    payload["workspace_mutator"] = {
        "status": "unresolved",
        "operation": "retry_reset",
    }

    errors = workspace_state_contract_errors(payload, operation)

    assert f"{operation} workspace has unresolved workspace mutator" in errors


def test_materialization_contract_accepts_running_workspace_mutator() -> None:
    payload = _valid_worktree_payload()
    payload["status"] = "running"
    payload["workspace_mutator"] = {
        "status": "unresolved",
        "operation": "success_finalizer",
    }

    errors = workspace_state_contract_errors(payload, "materialization")

    assert "unresolved workspace mutator" not in "; ".join(errors)


@pytest.mark.parametrize(
    ("evidence", "expected_error"),
    (
        ("unresolved", "workspace mutator evidence is invalid"),
        (
            {"status": "unknown", "operation": "success_finalizer"},
            "workspace mutator status is invalid",
        ),
        (
            {"status": "confirmed", "operation": "", "outcome": "finished"},
            "workspace mutator operation is invalid",
        ),
        (
            {"status": "confirmed", "operation": "retry_reset"},
            "confirmed workspace mutator outcome is invalid",
        ),
    ),
)
def test_workspace_contract_rejects_malformed_workspace_mutator_evidence(
    evidence: object,
    expected_error: str,
) -> None:
    payload = _valid_worktree_payload()
    payload["workspace_mutator"] = evidence

    errors = workspace_state_contract_errors(payload, "resume")

    assert expected_error in errors


def test_cleanup_contract_accepts_removed_publication_after_capture_failure() -> None:
    payload = _valid_worktree_payload()
    payload["status"] = "failed"
    payload.pop("result")
    payload.pop("refs")
    payload.pop("bundle")
    publication = payload["ref_publication"]
    assert isinstance(publication, dict)
    publication["phase"] = "removed"

    errors = workspace_state_contract_errors(payload, "cleanup")

    assert errors == ()


def test_cleanup_contract_rejects_publication_on_ordinary_non_lineage_state() -> None:
    payload = _valid_snapshot_payload()
    payload["ref_publication"] = deepcopy(_valid_worktree_payload()["ref_publication"])

    errors = workspace_state_contract_errors(payload, "ref_cleanup")

    assert "non-lineage workspace has ref publication evidence" in errors


def test_workspace_state_contract_validates_recursive_source_and_temporary_ref() -> (
    None
):
    payload = _valid_worktree_payload()
    project_source = deepcopy(payload["source"])
    payload["source"] = {
        "kind": "node",
        "node_id": "upstream",
        "commit": OID_C,
        "tree": OID_D,
        "candidate_sequence": 1,
        "bundle_path": "upstream/workspace-bundles/result.bundle",
        "bundle_sha256": SHA256,
        "bundle_size_bytes": 42,
        "bundle_ref": "refs/crewplane/runs/run-key/upstream/result",
        "upstream_sources": [project_source],
    }
    payload["invocation_source"] = {
        "source_kind": "node",
        "source_node_id": "upstream",
        "source_commit": OID_C,
        "source_tree": OID_D,
        "candidate_sequence": 1,
        "source_bundle_path": "upstream/workspace-bundles/result.bundle",
        "source_bundle_sha256": SHA256,
        "source_bundle_size_bytes": 42,
        "source_bundle_ref": "refs/crewplane/runs/run-key/upstream/result",
    }
    payload["temporary_refs"] = [
        {
            "phase": "prepared",
            "name": (
                "refs/crewplane/runs/run-key/imports/build/build-alpha-round1/source"
            ),
            "target_oid": OID_C,
            "owner_run_id": "run",
            "owner_node_id": "build",
            "owner_task_id": "alpha",
            "owner_role": "executor",
            "owner_round_num": 1,
            "owner_audit_round_num": None,
            "repository_id": "repo",
        }
    ]

    assert workspace_state_contract_is_valid(payload, "resume")

    source = payload["source"]
    assert isinstance(source, dict)
    source["upstream_sources"] = [None]
    assert (
        "node source upstream descriptor is invalid"
        in workspace_state_contract_errors(
            payload,
            "resume",
        )
    )


def test_workspace_state_contract_accepts_recursive_resume_publication_identity() -> (
    None
):
    payload = _valid_worktree_payload()
    publication = payload["ref_publication"]
    assert isinstance(publication, dict)
    publication["run_id"] = "source-run"
    publication["run_key_name"] = "source-key"
    destinations = publication["destinations"]
    assert isinstance(destinations, dict)
    for destination in destinations.values():
        assert isinstance(destination, dict)
        destination["name"] = str(destination["name"]).replace(
            "/run-key/", "/source-key/"
        )
    refs = payload["refs"]
    assert isinstance(refs, dict)
    refs.update(
        {
            label: destination["name"]
            for label, destination in destinations.items()
            if isinstance(destination, dict)
        }
    )
    payload["resume_origin"] = {
        "source_run_id": "intermediate-run",
        "source_run_key_name": "intermediate-key",
        "source_resume_origin": {
            "source_run_id": "source-run",
            "source_run_key_name": "source-key",
        },
    }

    assert workspace_state_contract_is_valid(payload, "resume")


def test_workspace_state_contract_error_requests_regeneration() -> None:
    payload = _valid_worktree_payload()
    payload["version"] = "other"

    with pytest.raises(RuntimeError, match="rerun or regenerate"):
        require_workspace_state_contract(payload, "resume")


@pytest.mark.parametrize("operation", ("resume", "rendering"))
def test_hydrated_lineage_allows_scrubbed_publication_evidence(
    operation: str,
) -> None:
    payload = _valid_worktree_payload()
    workspace = payload["workspace"]
    execution = payload["execution"]
    assert isinstance(workspace, dict)
    assert isinstance(execution, dict)
    payload["resume_origin"] = {
        "source_run_id": "source-run",
        "source_run_key_name": "source-key",
        "source_node_id": "build",
        "hydrated_at": "2026-08-29T00:00:00+00:00",
        "source_workspace": dict(workspace),
        "source_execution": dict(execution),
    }
    for field in ("path", "effective_cwd", "cache_root", "checkout_root", "cache_key"):
        workspace[field] = None
    workspace["retention"] = "not_applicable"
    workspace["retained_reason"] = "hydrated_resume"
    for field in (
        "cache_root",
        "workspace_path",
        "checkout_root",
        "effective_cwd",
        "worktree_git_dir",
    ):
        execution[field] = None
    payload.pop("ref_publication")

    require_workspace_state_contract(payload, operation)  # type: ignore[arg-type]


def _valid_worktree_payload() -> dict[str, object]:
    candidate_ref = "refs/crewplane/runs/run-key/build/build-alpha-round1/candidate"
    result_ref = "refs/crewplane/runs/run-key/build/build-alpha-round1/result"
    return {
        "version": SCHEMA_VERSION,
        "run_id": "run",
        "run_key_name": "run-key",
        "workflow_name": "workflow",
        "workflow_signature": SHA256,
        "node_id": "build",
        "task_id": "alpha",
        "provider": "mock",
        "role": "executor",
        "round_num": 1,
        "audit_round_num": None,
        "status": "succeeded",
        "workspace_kind": "worktree",
        "logical_worktree_name": "primary",
        "clean_start": "strict",
        "worktree_contract": {},
        "git": {
            "object_format": "sha1",
            "repo_id": "repo",
            "run_base_commit": OID_A,
            "source_tree": OID_B,
            "git_top_level": "/repo",
            "active_git_dir": "/repo/.git",
            "common_git_dir": "/repo/.git",
        },
        "source": {
            "kind": "project",
            "node_id": None,
            "commit": OID_A,
            "tree": OID_B,
            "candidate_sequence": None,
        },
        "invocation_source": {
            "source_kind": "project",
            "source_node_id": None,
            "source_commit": OID_A,
            "source_tree": OID_B,
            "candidate_sequence": None,
        },
        "workspace": {
            "cache_key": "alpha",
            "path": "/cache/workspace",
            "effective_cwd": "/cache/workspace/checkout",
            "materialization": "worktree_checkout",
            "writable": True,
            "lineage_producer": True,
            "retention": "retained",
            "retained_reason": None,
            "project_root_relative_path": ".",
            "reuse_generation": 1,
        },
        "execution": {
            "workspace_path": "/cache/workspace",
            "checkout_root": "/cache/workspace/checkout",
            "effective_cwd": "/cache/workspace/checkout",
            "worktree_git_dir": "/repo/.git/worktrees/workspace",
        },
        "process_drain": {"status": "confirmed"},
        "result": {
            "candidate_commit": OID_C,
            "result_commit": OID_D,
            "candidate_tree": OID_B,
            "result_tree": OID_B,
            "changed_path_count": 1,
        },
        "refs": {"candidate": candidate_ref, "result": result_ref},
        "bundle": {
            "path": "build/workspace-bundles/result.bundle",
            "sha256": SHA256,
            "size_bytes": 42,
            "verified": True,
        },
        "ref_publication": {
            "phase": "published",
            "repository_id": "repo",
            "run_id": "run",
            "run_key_name": "run-key",
            "node_id": "build",
            "task_id": "alpha",
            "role": "executor",
            "round_num": 1,
            "audit_round_num": None,
            "destinations": {
                "candidate": {
                    "name": candidate_ref,
                    "target_oid": OID_C,
                    "expected_old_oid": None,
                },
                "result": {
                    "name": result_ref,
                    "target_oid": OID_D,
                    "expected_old_oid": None,
                },
            },
        },
    }


def _valid_snapshot_payload() -> dict[str, object]:
    payload = _valid_worktree_payload()
    payload["workspace_kind"] = "snapshot"
    workspace = payload["workspace"]
    assert isinstance(workspace, dict)
    workspace["materialization"] = "snapshot_checkout"
    workspace["lineage_producer"] = False
    workspace.pop("reuse_generation")
    payload["result"] = {
        "drift_scan_complete": True,
        "snapshot_drift_discarded": False,
        "changed_path_count": 0,
        "changed_paths": [],
        "changed_paths_truncated": False,
    }
    payload.pop("bundle")
    payload.pop("ref_publication")
    return payload


def _set_path(payload: dict[str, object], path: tuple[str, ...], value: object) -> None:
    current = payload
    for part in path[:-1]:
        nested = current[part]
        assert isinstance(nested, dict)
        current = nested
    if value is DELETE:
        current.pop(path[-1])
    else:
        current[path[-1]] = value

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
from crewplane.core.workspace.invocation_identity import invocation_slug
from tests.unit.artifacts.workspace_state_contracts_support import (
    OID_C,
    OID_D,
    SHA256,
    valid_snapshot_payload,
    valid_worktree_payload,
)

DELETE: Final = object()


@pytest.mark.parametrize(
    ("path", "value", "expected_error", "operation"),
    [
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
        (
            ("execution", "workspace_path"),
            None,
            "failed_invocation workspace lacks its physical path",
            "failed_invocation",
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
    ],
)
def test_workspace_state_contract_rejects_contradictory_evidence(
    path: tuple[str, ...],
    value: object,
    expected_error: str,
    operation: PersistedWorkspaceOperation,
) -> None:
    payload = valid_worktree_payload()
    _set_path(payload, path, value)

    errors = workspace_state_contract_errors(payload, operation)

    assert expected_error in errors


def test_workspace_state_contract_validates_snapshot_reporting_shape() -> None:
    payload = valid_snapshot_payload()
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


def test_workspace_state_contract_validates_recursive_source_and_temporary_ref() -> (
    None
):
    payload = valid_worktree_payload()
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
    slug = invocation_slug("build", "alpha", None, 1)
    payload["temporary_refs"] = [
        {
            "phase": "prepared",
            "name": (f"refs/crewplane/runs/run-key/imports/build/{slug}/source"),
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
    payload = valid_worktree_payload()
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
    payload = valid_worktree_payload()
    payload["version"] = "other"

    with pytest.raises(RuntimeError, match="rerun or regenerate"):
        require_workspace_state_contract(payload, "resume")


@pytest.mark.parametrize("operation", ["resume", "rendering", "failed_invocation"])
def test_hydrated_lineage_allows_scrubbed_publication_evidence(
    operation: PersistedWorkspaceOperation,
) -> None:
    payload = valid_worktree_payload()
    if operation == "failed_invocation":
        payload["status"] = "failed"
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

    require_workspace_state_contract(payload, operation)
    assert (
        "cleanup workspace lacks its physical path"
        in workspace_state_contract_errors(payload, "cleanup")
    )

    payload.pop("resume_origin")
    assert not workspace_state_contract_is_valid(payload, operation)


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

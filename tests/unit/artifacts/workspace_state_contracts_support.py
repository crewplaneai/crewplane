from __future__ import annotations

from typing import Final

from crewplane.core.workspace.invocation_identity import invocation_slug
from crewplane.version import SCHEMA_VERSION

OID_A: Final = "a" * 40


OID_B: Final = "b" * 40


OID_C: Final = "c" * 40


OID_D: Final = "d" * 40


SHA256: Final = "e" * 64


def valid_worktree_payload() -> dict[str, object]:
    slug = invocation_slug("build", "alpha", None, 1)
    candidate_ref = f"refs/crewplane/runs/run-key/build/{slug}/candidate"
    result_ref = f"refs/crewplane/runs/run-key/build/{slug}/result"
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


def valid_snapshot_payload() -> dict[str, object]:
    payload = valid_worktree_payload()
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

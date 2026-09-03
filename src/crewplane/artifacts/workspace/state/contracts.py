from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from crewplane.core.value_checks import is_nonnegative_int
from crewplane.version import SCHEMA_VERSION

from .fields import is_hex_object, is_nonempty_string, mapping_value
from .ref_contracts import validate_ref_contracts

PersistedWorkspaceOperation = Literal[
    "materialization",
    "resume",
    "duplicate_skip",
    "rendering",
    "export",
    "cleanup",
    "ref_cleanup",
]

_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def require_workspace_state_contract(
    payload: dict[str, object],
    operation: PersistedWorkspaceOperation,
) -> None:
    errors = workspace_state_contract_errors(payload, operation)
    if errors:
        detail = "; ".join(errors)
        raise RuntimeError(
            "Workspace state lacks required hardening evidence; rerun or "
            f"regenerate the workspace artifacts: {detail}."
        )


def workspace_state_contract_is_valid(
    payload: dict[str, object],
    operation: PersistedWorkspaceOperation,
) -> bool:
    return not workspace_state_contract_errors(payload, operation)


def workspace_state_contract_errors(
    payload: dict[str, object],
    operation: PersistedWorkspaceOperation,
) -> tuple[str, ...]:
    errors: list[str] = []
    _validate_identity(payload, errors)
    workspace = mapping_value(payload.get("workspace"))
    git = mapping_value(payload.get("git"))
    source = mapping_value(payload.get("source"))
    _validate_repository(git, errors)
    _validate_workspace(payload, workspace, operation, errors)
    _validate_source(source, errors)
    _validate_invocation_source(payload, source, errors)
    _validate_process_drain(payload, operation, errors)
    _validate_workspace_mutator(payload, operation, errors)
    _validate_result(payload, workspace, operation, errors)
    validate_ref_contracts(
        payload, errors, _hydrated_resume_placement(payload, workspace)
    )
    return tuple(errors)


def _validate_identity(payload: Mapping[str, object], errors: list[str]) -> None:
    if payload.get("version") != SCHEMA_VERSION:
        errors.append("schema version mismatch")
    for field in (
        "run_id",
        "run_key_name",
        "workflow_name",
        "workflow_signature",
        "node_id",
        "task_id",
        "provider",
    ):
        if not is_nonempty_string(payload.get(field)):
            errors.append(f"missing {field}")
    if payload.get("role") not in {"executor", "reviewer"}:
        errors.append("invalid provider role")
    if not is_nonnegative_int(payload.get("round_num")):
        errors.append("invalid round_num")


def _validate_repository(git: Mapping[str, object], errors: list[str]) -> None:
    if git.get("object_format") not in {"sha1", "sha256"}:
        errors.append("invalid object format")
    for field in (
        "repo_id",
        "run_base_commit",
        "source_tree",
        "git_top_level",
        "active_git_dir",
        "common_git_dir",
    ):
        if not is_nonempty_string(git.get(field)):
            errors.append(f"missing git.{field}")


def _validate_workspace(
    payload: Mapping[str, object],
    workspace: Mapping[str, object],
    operation: PersistedWorkspaceOperation,
    errors: list[str],
) -> None:
    _validate_workspace_materialization(payload, workspace, errors)
    _validate_workspace_operation_eligibility(payload, operation, errors)
    _validate_workspace_retention(payload, workspace, errors)


def _validate_workspace_materialization(
    payload: Mapping[str, object],
    workspace: Mapping[str, object],
    errors: list[str],
) -> None:
    match payload.get("workspace_kind"):
        case "snapshot":
            _validate_snapshot_materialization(workspace, errors)
        case "worktree":
            _validate_worktree_materialization(payload, workspace, errors)
        case _:
            errors.append("invalid workspace kind")
    if workspace.get("writable") is not True:
        errors.append("managed workspace must be writable")


def _validate_snapshot_materialization(
    workspace: Mapping[str, object],
    errors: list[str],
) -> None:
    if workspace.get("materialization") != "snapshot_checkout":
        errors.append("snapshot materialization mismatch")
    if workspace.get("lineage_producer") is not False:
        errors.append("snapshot cannot produce lineage")
    if "reuse_generation" in workspace:
        errors.append("snapshot cannot claim a reuse generation")


def _validate_worktree_materialization(
    payload: Mapping[str, object],
    workspace: Mapping[str, object],
    errors: list[str],
) -> None:
    if workspace.get("materialization") != "worktree_checkout":
        errors.append("worktree materialization mismatch")
    execution = mapping_value(payload.get("execution"))
    if execution.get("effective_cwd") is None:
        return
    if not _positive_int(workspace.get("reuse_generation")):
        errors.append("materialized worktree lacks reuse generation")
    if not is_nonempty_string(execution.get("worktree_git_dir")):
        errors.append("materialized worktree lacks exact Git directory")


def _validate_workspace_operation_eligibility(
    payload: Mapping[str, object],
    operation: PersistedWorkspaceOperation,
    errors: list[str],
) -> None:
    status = payload.get("status")
    if operation == "cleanup" and not is_nonempty_string(
        mapping_value(payload.get("execution")).get("workspace_path")
    ):
        errors.append("cleanup workspace lacks its physical path")
    if (
        operation in {"resume", "duplicate_skip", "rendering", "export"}
        and status != "succeeded"
    ):
        errors.append(f"{operation} requires a succeeded workspace")
    if operation == "cleanup" and status not in _TERMINAL_STATUSES:
        errors.append("cleanup requires a terminal outcome")


def _validate_workspace_retention(
    payload: Mapping[str, object],
    workspace: Mapping[str, object],
    errors: list[str],
) -> None:
    status = payload.get("status")
    retention = workspace.get("retention")
    if (
        status in _TERMINAL_STATUSES
        and retention not in {"pending_cleanup", "deleted", "retained"}
        and not _hydrated_resume_placement(payload, workspace)
    ):
        errors.append("terminal workspace has invalid retention")


def _validate_source(source: Mapping[str, object], errors: list[str]) -> None:
    kind = source.get("kind")
    match kind:
        case "project":
            _validate_project_source(source, errors)
        case "node" | "candidate":
            _validate_bundled_source(source, kind, errors)
        case _:
            errors.append("invalid source kind")
    if not is_hex_object(source.get("commit")) or not is_hex_object(source.get("tree")):
        errors.append("source commit or tree is invalid")


def _validate_project_source(
    source: Mapping[str, object],
    errors: list[str],
) -> None:
    if source.get("node_id") is not None:
        errors.append("project source cannot name a source node")
    upstreams = source.get("upstream_sources")
    if upstreams is not None and upstreams != []:
        errors.append("project source cannot contain upstream sources")
    if any(
        source.get(field) is not None
        for field in (
            "bundle_path",
            "bundle_sha256",
            "bundle_size_bytes",
            "bundle_ref",
        )
    ):
        errors.append("project source cannot contain bundle evidence")


def _validate_bundled_source(
    source: Mapping[str, object],
    kind: str,
    errors: list[str],
) -> None:
    if not is_nonempty_string(source.get("node_id")):
        errors.append(f"{kind} source requires a source node")
    if not is_nonempty_string(source.get("bundle_path")):
        errors.append(f"{kind} source lacks bundle_path")
    if not _sha256(source.get("bundle_sha256")):
        errors.append(f"{kind} source lacks bundle_sha256")
    if not is_nonnegative_int(source.get("bundle_size_bytes")):
        errors.append(f"{kind} source lacks bundle_size_bytes")
    if not is_nonempty_string(source.get("bundle_ref")):
        errors.append(f"{kind} source lacks bundle_ref")
    _validate_upstream_source(source.get("upstream_sources"), kind, errors)


def _validate_upstream_source(
    upstreams: object,
    kind: str,
    errors: list[str],
) -> None:
    if not isinstance(upstreams, list) or len(upstreams) != 1:
        errors.append(f"{kind} source requires exactly one upstream source")
        return
    upstream = upstreams[0]
    if not isinstance(upstream, dict):
        errors.append(f"{kind} source upstream descriptor is invalid")
        return
    _validate_source(upstream, errors)


def _validate_invocation_source(
    payload: Mapping[str, object],
    source: Mapping[str, object],
    errors: list[str],
) -> None:
    invocation = mapping_value(payload.get("invocation_source"))
    expected = {
        "source_kind": source.get("kind"),
        "source_node_id": source.get("node_id"),
        "source_commit": source.get("commit"),
        "source_tree": source.get("tree"),
        "candidate_sequence": source.get("candidate_sequence"),
    }
    for field, expected_value in expected.items():
        if invocation.get(field) != expected_value:
            errors.append(f"invocation source {field} mismatch")
    for field in ("bundle_path", "bundle_sha256", "bundle_size_bytes", "bundle_ref"):
        if invocation.get(f"source_{field}") != source.get(field):
            errors.append(f"invocation source {field} mismatch")


def _validate_process_drain(
    payload: Mapping[str, object],
    operation: PersistedWorkspaceOperation,
    errors: list[str],
) -> None:
    process_drain = mapping_value(payload.get("process_drain"))
    drain_status = process_drain.get("status")
    if drain_status not in {
        "not_started",
        "confirmed",
        "unresolved",
    }:
        errors.append("missing process drain evidence")
    if payload.get("status") == "succeeded" and drain_status == "unresolved":
        errors.append("successful workspace has unresolved process liveness")
    if operation == "cleanup" and drain_status == "unresolved":
        errors.append("cleanup workspace has unresolved process liveness")


def _validate_workspace_mutator(
    payload: Mapping[str, object],
    operation: PersistedWorkspaceOperation,
    errors: list[str],
) -> None:
    value = payload.get("workspace_mutator")
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append("workspace mutator evidence is invalid")
        return
    status = value.get("status")
    if status not in {"confirmed", "unresolved"}:
        errors.append("workspace mutator status is invalid")
    if not is_nonempty_string(value.get("operation")):
        errors.append("workspace mutator operation is invalid")
    if status == "confirmed" and value.get("outcome") != "finished":
        errors.append("confirmed workspace mutator outcome is invalid")
    if payload.get("status") == "succeeded" and status == "unresolved":
        errors.append("successful workspace has unresolved workspace mutator")
    if operation in {"cleanup", "ref_cleanup"} and status == "unresolved":
        errors.append(f"{operation} workspace has unresolved workspace mutator")


def _validate_result(
    payload: Mapping[str, object],
    workspace: Mapping[str, object],
    operation: PersistedWorkspaceOperation,
    errors: list[str],
) -> None:
    if operation == "materialization" or payload.get("status") != "succeeded":
        return
    result = mapping_value(payload.get("result"))
    if payload.get("workspace_kind") == "snapshot":
        _validate_snapshot_result(result, errors)
        return
    if workspace.get("lineage_producer") is not True:
        return
    _validate_lineage_result(result, errors)
    _validate_bundle_evidence(mapping_value(payload.get("bundle")), errors)


def _validate_snapshot_result(
    result: Mapping[str, object],
    errors: list[str],
) -> None:
    complete = result.get("drift_scan_complete")
    if not isinstance(complete, bool):
        errors.append("snapshot result lacks drift scan completeness")
    if complete is False and any(
        field in result
        for field in (
            "snapshot_drift_discarded",
            "changed_path_count",
            "changed_paths",
            "changed_paths_truncated",
        )
    ):
        errors.append("incomplete snapshot drift contains exact claims")


def _validate_lineage_result(
    result: Mapping[str, object],
    errors: list[str],
) -> None:
    for field in ("candidate_commit", "result_commit", "candidate_tree", "result_tree"):
        if not is_hex_object(result.get(field)):
            errors.append(f"lineage result lacks {field}")
    if not is_nonnegative_int(result.get("changed_path_count")):
        errors.append("lineage result lacks changed_path_count")


def _validate_bundle_evidence(
    bundle: Mapping[str, object],
    errors: list[str],
) -> None:
    if not (
        is_nonempty_string(bundle.get("path"))
        and is_nonempty_string(bundle.get("sha256"))
        and is_nonnegative_int(bundle.get("size_bytes"))
        and bundle.get("verified") is True
    ):
        errors.append("lineage result lacks verified bundle evidence")


def _hydrated_resume_placement(
    payload: Mapping[str, object],
    workspace: Mapping[str, object],
) -> bool:
    execution = mapping_value(payload.get("execution"))
    origin = mapping_value(payload.get("resume_origin"))
    return (
        workspace.get("retention") == "not_applicable"
        and workspace.get("retained_reason") == "hydrated_resume"
        and all(
            workspace.get(field) is None
            for field in (
                "path",
                "effective_cwd",
                "cache_root",
                "checkout_root",
                "cache_key",
            )
        )
        and all(
            execution.get(field) is None
            for field in (
                "cache_root",
                "workspace_path",
                "checkout_root",
                "effective_cwd",
                "worktree_git_dir",
            )
        )
        and is_nonempty_string(origin.get("source_run_id"))
        and is_nonempty_string(origin.get("source_run_key_name"))
        and origin.get("source_node_id") == payload.get("node_id")
        and is_nonempty_string(origin.get("hydrated_at"))
        and isinstance(origin.get("source_workspace"), dict)
        and isinstance(origin.get("source_execution"), dict)
    )


def _positive_int(value: object) -> bool:
    return is_nonnegative_int(value) and value > 0


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )

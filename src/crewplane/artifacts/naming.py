from __future__ import annotations

import hashlib
import json
import re

from crewplane.architecture.contracts import artifacts as _artifact_contracts
from crewplane.core.workflow.keywords import ProviderRole

MAX_GENERATED_PATH_COMPONENT_CHARS = (
    _artifact_contracts.MAX_ARTIFACT_PATH_COMPONENT_CHARS
)
MAX_GENERATED_FILE_RESULT_DIR_CHARS = 120
GENERATED_FILE_RESULT_DIR_HASH_CHARS = 12

_RUN_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

build_findings_filename = _artifact_contracts.build_findings_filename
build_result_filename = _artifact_contracts.build_result_filename
build_stage_directory_name = _artifact_contracts.build_stage_directory_name
safe_artifact_name = _artifact_contracts.safe_artifact_name
safe_stage_name = _artifact_contracts.safe_stage_name


def workflow_identity_hash(workflow_identity: str) -> str:
    return _artifact_contracts.artifact_name_hash(workflow_identity)


def build_lock_name(
    workflow_name: str,
    workflow_identity: str,
    workflow_signature: str,
) -> str:
    suffix = f"--{workflow_identity_hash(workflow_identity)}--{workflow_signature}.lock"
    return _artifact_contracts.bounded_artifact_name(
        safe_artifact_name(workflow_name), suffix
    )


def build_run_key_name(workflow_name: str, run_id: str) -> str:
    suffix = f"--{_artifact_contracts.artifact_name_hash(workflow_name)}-{run_id}"
    return _artifact_contracts.bounded_artifact_name(
        safe_artifact_name(workflow_name), suffix
    )


def validate_run_key_name(run_key_name: str) -> str:
    if (
        not run_key_name
        or len(run_key_name) > MAX_GENERATED_PATH_COMPONENT_CHARS
        or not _RUN_KEY_PATTERN.fullmatch(run_key_name)
    ):
        raise ValueError("run_key_name must be a bounded generated path component.")
    return run_key_name


def build_node_state_filename(node_id: str) -> str:
    suffix = f"--{_artifact_contracts.artifact_name_hash(node_id)}.json"
    return _artifact_contracts.bounded_artifact_name(safe_stage_name(node_id), suffix)


def build_provider_process_state_filename(
    node_id: str,
    task_id: str,
    provider: str,
    role: ProviderRole,
    audit_round_num: int | None,
    round_num: int,
    attempt: int,
) -> str:
    identity = json.dumps(
        [
            node_id,
            task_id,
            provider,
            str(role),
            audit_round_num,
            round_num,
            attempt,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    suffix = f"--{_artifact_contracts.artifact_name_hash(identity)}.json"
    prefix = safe_stage_name(f"{node_id}-{task_id}")
    return _artifact_contracts.bounded_artifact_name(prefix, suffix)


def build_workspace_export_filename(logical_worktree_name: str) -> str:
    suffix = f"--{_artifact_contracts.artifact_name_hash(logical_worktree_name)}.json"
    return _artifact_contracts.bounded_artifact_name(
        safe_artifact_name(logical_worktree_name), suffix
    )


def build_generated_file_result_dir_name(name: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    if not safe_name:
        return "stage"
    if len(safe_name) <= MAX_GENERATED_FILE_RESULT_DIR_CHARS:
        return safe_name
    digest = hashlib.sha256(name.encode()).hexdigest()
    suffix = f"-{digest[:GENERATED_FILE_RESULT_DIR_HASH_CHARS]}"
    available = MAX_GENERATED_FILE_RESULT_DIR_CHARS - len(suffix)
    prefix = safe_name[:available].rstrip(".-")
    return f"{prefix or 'stage'}{suffix}"


def build_log_filename(
    task_id: str,
    audit_round_num: int | None = None,
    round_num: int | None = None,
) -> str:
    audit_part = f"-audit{audit_round_num}" if audit_round_num is not None else ""
    round_part = f"-round{round_num}" if round_num is not None else ""
    suffix = f"{audit_part}{round_part}.log"
    safe_name = safe_artifact_name(task_id)
    if len(f"{safe_name}{suffix}") <= MAX_GENERATED_PATH_COMPONENT_CHARS:
        return f"{safe_name}{suffix}"
    return _artifact_contracts.bounded_artifact_name(
        safe_name, f"--{_artifact_contracts.artifact_name_hash(task_id)}{suffix}"
    )

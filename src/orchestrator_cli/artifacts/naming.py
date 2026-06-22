from __future__ import annotations

import hashlib
import re

MAX_GENERATED_PATH_COMPONENT_CHARS = 180
MAX_GENERATED_FILE_RESULT_DIR_CHARS = 120
GENERATED_FILE_RESULT_DIR_HASH_CHARS = 12

_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
_STAGE_PATTERN = re.compile(r"[^a-z0-9._-]+")
_RUN_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def safe_artifact_name(name: str) -> str:
    return _slugify_name(name, _SLUG_PATTERN, strip_edges=True)


def safe_stage_name(name: str) -> str:
    return _slugify_name(name, _STAGE_PATTERN, strip_edges=False)


def workflow_identity_hash(workflow_identity: str) -> str:
    return _short_hash(workflow_identity)


def build_lock_name(
    workflow_name: str,
    workflow_identity: str,
    workflow_signature: str,
) -> str:
    suffix = f"--{workflow_identity_hash(workflow_identity)}--{workflow_signature}.lock"
    return _bounded_with_suffix(safe_artifact_name(workflow_name), suffix)


def build_run_key_name(workflow_name: str, run_id: str) -> str:
    suffix = f"--{_short_hash(workflow_name)}-{run_id}"
    return _bounded_with_suffix(safe_artifact_name(workflow_name), suffix)


def validate_run_key_name(run_key_name: str) -> str:
    if (
        not run_key_name
        or len(run_key_name) > MAX_GENERATED_PATH_COMPONENT_CHARS
        or not _RUN_KEY_PATTERN.fullmatch(run_key_name)
    ):
        raise ValueError("run_key_name must be a bounded generated path component.")
    return run_key_name


def build_node_state_filename(node_id: str) -> str:
    suffix = f"--{_short_hash(node_id)}.json"
    return _bounded_with_suffix(safe_stage_name(node_id), suffix)


def build_workspace_export_filename(logical_worktree_name: str) -> str:
    suffix = f"--{_short_hash(logical_worktree_name)}.json"
    return _bounded_with_suffix(safe_artifact_name(logical_worktree_name), suffix)


def build_stage_directory_name(node_id: str) -> str:
    safe_name = safe_stage_name(node_id)
    if len(safe_name) <= MAX_GENERATED_PATH_COMPONENT_CHARS:
        return safe_name
    return _bounded_with_suffix(safe_name, f"--{_short_hash(node_id)}")


def build_result_filename(node_id: str) -> str:
    return _bounded_artifact_filename(node_id, "-result.md")


def build_findings_filename(node_id: str) -> str:
    return _bounded_artifact_filename(node_id, "-findings.md")


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
    return _bounded_with_suffix(safe_name, f"--{_short_hash(task_id)}{suffix}")


def _bounded_artifact_filename(node_id: str, suffix: str) -> str:
    safe_name = safe_stage_name(node_id)
    if len(f"{safe_name}{suffix}") <= MAX_GENERATED_PATH_COMPONENT_CHARS:
        return f"{safe_name}{suffix}"
    return _bounded_with_suffix(safe_name, f"--{_short_hash(node_id)}{suffix}")


def _bounded_with_suffix(safe_prefix: str, suffix: str) -> str:
    if len(suffix) >= MAX_GENERATED_PATH_COMPONENT_CHARS:
        raise ValueError("Generated suffix exceeds path component budget.")
    available = MAX_GENERATED_PATH_COMPONENT_CHARS - len(suffix)
    prefix = safe_prefix[:available].rstrip("-._")
    if not prefix:
        prefix = "artifact"[:available]
    return f"{prefix}{suffix}"


def _slugify_name(
    name: str,
    pattern: re.Pattern[str],
    strip_edges: bool,
) -> str:
    stripped = name.strip().lower()
    if not stripped or stripped in {".", ".."}:
        return "task"
    slug = pattern.sub("-", stripped)
    if strip_edges:
        slug = slug.strip("-")
    if slug in {".", ".."}:
        return "task"
    if not slug.strip("-._"):
        return "task" if strip_edges else slug
    return slug or "task"


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]

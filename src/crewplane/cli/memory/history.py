"""Resolve retained parent runs and verify memory extraction eligibility."""

from __future__ import annotations

from pathlib import Path

from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.architecture.safe_files import contained_directory
from crewplane.artifacts.locks import run_lock_activity
from crewplane.artifacts.memory.completion import read_completion
from crewplane.artifacts.memory.selections import validate_memory_selection_receipt
from crewplane.artifacts.naming import validate_run_key_name
from crewplane.cli.run.completion import CompletedRun
from crewplane.core.execution_state import RunManifest, TerminalRunStatus
from crewplane.core.preflight.models import PreflightExecutionPlan

_PARENT_ARTIFACT_MAX_BYTES = 16 * 1024 * 1024


def completed_parent(project_root: Path, selector: str) -> CompletedRun:
    """Load a completed parent eligible for explicit memory extraction.

    The selector must uniquely match a stage-root entry by exact artifact key
    or hyphen-prefixed suffix (normally a run ID). Exact matches do not take
    precedence over suffix matches.

    The manifest and frozen plan must identify the same succeeded or failed
    run, with matching memory selections, no current run lock, and retained
    proof of manifest commit, cleanup, own-handle release, and result display.
    No artifacts are modified and no execution lock is acquired.

    Raises:
        ValueError: The selector or retained artifacts are invalid, ambiguous,
            or do not prove eligibility.
        OSError: A protected artifact read or directory lookup fails.
    """
    stages = _find_parent_stages(project_root, selector)
    state_dir = project_root / ".crewplane"
    manifest = RunManifest.model_validate_json(
        read_contained_bytes(stages, "manifests/run.json", _PARENT_ARTIFACT_MAX_BYTES)
    )
    plan = PreflightExecutionPlan.model_validate_json(
        read_contained_bytes(
            stages, "preflight/execution-plan.json", _PARENT_ARTIFACT_MAX_BYTES
        )
    )
    outcome = _validate_parent_eligibility(state_dir, stages, manifest, plan)
    return CompletedRun(
        "executed",
        project_root,
        state_dir,
        plan,
        stages,
        state_dir / "execution-results" / stages.name,
        outcome,
        manifest_committed=True,
        cleanup_confirmed=True,
        lock_released=True,
        result_displayed=True,
    )


def _find_parent_stages(project_root: Path, selector: str) -> Path:
    stages_root = contained_directory(project_root, ".crewplane/execution-stages")
    if stages_root is None or "/" in selector or "\\" in selector or not selector:
        raise ValueError(
            "Choose an existing parent run ID or artifact key from this project."
        )
    matches = [
        path
        for path in stages_root.iterdir()
        if path.name == selector or path.name.endswith("-" + selector)
    ]
    if len(matches) != 1:
        raise ValueError(
            "Run selector is missing or ambiguous; use the exact parent artifact key."
        )
    key = validate_run_key_name(matches[0].name)
    return stages_root / key


def _validate_parent_eligibility(
    state_dir: Path,
    stages: Path,
    manifest: RunManifest,
    plan: PreflightExecutionPlan,
) -> TerminalRunStatus:
    key = stages.name
    validate_memory_selection_receipt(plan, stages)
    if (
        manifest.status not in {"succeeded", "failed"}
        or manifest.run_id != plan.run_id
        or manifest.workflow_signature != plan.workflow_signature
        or manifest.run_key_name != key
        or plan.run_key_name != key
    ):
        raise ValueError(
            "Explicit memory extraction requires a matching completed non-cancelled parent run."
        )
    if run_lock_activity(state_dir, key) != "none":
        raise ValueError(
            "Parent execution lock release is unconfirmed; resolve the existing run cleanup before extraction."
        )
    completion = read_completion(stages)
    if not completion.eligible or (
        completion.run_id,
        completion.run_key_name,
        completion.workflow_signature,
        completion.outcome,
    ) != (plan.run_id, key, plan.workflow_signature, manifest.status):
        raise ValueError(
            "Retained completion proof does not confirm matching parent outcome, cleanup, own-handle release, and result display; extraction is ineligible."
        )
    return manifest.status

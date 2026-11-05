from __future__ import annotations

from pathlib import Path

from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.architecture.safe_files import contained_directory
from crewplane.artifacts.locks import run_lock_activity
from crewplane.artifacts.memory.completion import read_completion
from crewplane.artifacts.memory.selections import validate_memory_selection_receipt
from crewplane.artifacts.naming import validate_run_key_name
from crewplane.cli.run.completion import CompletedRun
from crewplane.core.execution_state import RunManifest
from crewplane.core.preflight.models import PreflightExecutionPlan


def completed_parent(project_root: Path, selector: str) -> CompletedRun:
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
    stages = stages_root / key
    state_dir = project_root / ".crewplane"
    manifest = RunManifest.model_validate_json(
        read_contained_bytes(stages, "manifests/run.json", 16 * 1024 * 1024)
    )
    plan = PreflightExecutionPlan.model_validate_json(
        read_contained_bytes(stages, "preflight/execution-plan.json", 16 * 1024 * 1024)
    )
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
    return CompletedRun(
        "executed",
        project_root,
        state_dir,
        plan,
        stages,
        state_dir / "execution-results" / key,
        manifest.status,
        manifest_committed=True,
        cleanup_confirmed=True,
        lock_released=True,
        result_displayed=True,
    )

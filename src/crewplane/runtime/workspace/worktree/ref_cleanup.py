from __future__ import annotations

import json
import shutil
import stat
import subprocess
from collections.abc import Callable
from os import scandir
from pathlib import Path

from crewplane.artifacts.workspace.state.contracts import (
    require_workspace_state_contract,
)
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.workflow.keywords import RESERVED_RUN_ROOT_NAMES
from crewplane.core.workspace.repository_identity import workspace_repository_id

from ..git import git
from ..state import read_workspace_state
from .ref_publication import reconcile_result_ref_publication
from .temporary_refs import reconcile_temporary_import_refs

WorkspaceRunRefCleanup = Callable[[str], int]


def workspace_ref_cleanup_for_project(
    project_root: Path,
) -> WorkspaceRunRefCleanup | None:
    if shutil.which("git") is None:
        return None
    try:
        repo_root = Path(
            git(project_root).text("rev-parse", "--show-toplevel")
        ).resolve(strict=False)
        common_git_dir = resolve_common_git_dir(repo_root)
    except subprocess.CalledProcessError:
        return None

    def cleanup_run_refs(run_key_name: str) -> int:
        return delete_run_workspace_refs(
            repo_root,
            common_git_dir,
            project_root,
            run_key_name,
            project_root / ".crewplane" / "execution-stages" / run_key_name,
        )

    return cleanup_run_refs


def delete_run_workspace_refs(
    repo_root: Path,
    common_git_dir: Path,
    project_root: Path,
    run_key_name: str,
    run_dir: Path | None = None,
) -> int:
    if run_dir is None or not run_dir.is_dir() or run_dir.is_symlink():
        return 0
    repository_id = _current_repository_id(
        repo_root,
        common_git_dir,
        project_root,
    )
    evidence_paths: list[Path] = []
    dedicated_paths: set[Path] = set()
    for evidence_path in _ref_cleanup_evidence_paths(run_dir, run_key_name):
        if not evidence_path.is_file() or evidence_path.is_symlink():
            raise RuntimeError(
                f"Workspace ref cleanup found unsafe evidence: {evidence_path}."
            )
        try:
            payload = read_workspace_state(evidence_path)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Workspace ref cleanup found malformed evidence: {evidence_path}."
            ) from exc
        if payload.get("run_key_name") != run_key_name:
            raise RuntimeError(
                "Workspace ref cleanup found contradictory run evidence: "
                f"{evidence_path}."
            )
        _require_ref_cleanup_repository_identity(
            payload,
            evidence_path,
            repository_id,
        )
        if payload.get("evidence_kind") == "temporary_ref_cleanup":
            _require_temporary_ref_cleanup_evidence(payload, evidence_path)
            dedicated_paths.add(evidence_path)
        else:
            require_workspace_state_contract(payload, "ref_cleanup")
            _require_drained_ref_cleanup_state(payload, evidence_path)
        evidence_paths.append(evidence_path)
    removed = 0
    for evidence_path in evidence_paths:
        if evidence_path not in dedicated_paths:
            removed += reconcile_result_ref_publication(
                evidence_path,
                repo_root,
                common_git_dir,
            )
        removed += reconcile_temporary_import_refs(
            evidence_path,
            repo_root,
            common_git_dir,
            repository_id,
        )
        if evidence_path in dedicated_paths:
            evidence_path.unlink()
    return removed


def _ref_cleanup_evidence_paths(
    run_dir: Path,
    run_key_name: str,
) -> tuple[Path, ...]:
    paths: list[Path] = []
    stage_paths: set[Path] = set()
    try:
        with scandir(run_dir) as entries:
            stage_entries = tuple(entries)
        for stage_entry in stage_entries:
            if stage_entry.name in RESERVED_RUN_ROOT_NAMES:
                if stage_entry.name == "logs":
                    paths.extend(
                        _run_log_temporary_ref_evidence(Path(stage_entry.path))
                    )
                continue
            stage_path = Path(stage_entry.path)
            if stage_entry.is_symlink():
                raise RuntimeError(
                    f"Workspace ref cleanup found unsafe evidence: {stage_path}."
                )
            if not stage_entry.is_dir(follow_symlinks=False):
                continue
            stage_paths.add(stage_path)
        stage_paths.update(_planned_stage_paths(run_dir, run_key_name))
        for stage_path in sorted(stage_paths):
            with scandir(stage_path) as entries:
                paths.extend(
                    Path(entry.path)
                    for entry in entries
                    if _is_ref_cleanup_evidence_name(entry.name)
                )
    except OSError as exc:
        raise RuntimeError(
            f"Workspace ref cleanup could not scan evidence beneath {run_dir}."
        ) from exc
    return tuple(sorted(paths))


def _run_log_temporary_ref_evidence(
    logs_path: Path,
) -> tuple[Path, ...]:
    try:
        mode = logs_path.lstat().st_mode
    except OSError as exc:
        raise RuntimeError(
            f"Workspace ref cleanup found unsafe evidence directory: {logs_path}."
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeError(
            f"Workspace ref cleanup found unsafe evidence directory: {logs_path}."
        )
    with scandir(logs_path) as entries:
        return tuple(
            Path(entry.path)
            for entry in entries
            if _is_temporary_ref_evidence_name(entry.name)
        )


def _planned_stage_paths(run_dir: Path, run_key_name: str) -> tuple[Path, ...]:
    preflight_path = run_dir / "preflight"
    plan_path = preflight_path / "execution-plan.json"
    try:
        preflight_mode = preflight_path.lstat().st_mode
        plan_stat = plan_path.lstat()
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise RuntimeError(
            f"Workspace ref cleanup could not read the plan beneath {run_dir}."
        ) from exc
    if (
        stat.S_ISLNK(preflight_mode)
        or not stat.S_ISDIR(preflight_mode)
        or stat.S_ISLNK(plan_stat.st_mode)
        or not stat.S_ISREG(plan_stat.st_mode)
        or plan_stat.st_nlink != 1
    ):
        raise RuntimeError(
            f"Workspace ref cleanup found unsafe plan evidence: {plan_path}."
        )
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"Workspace ref cleanup found malformed plan evidence: {plan_path}."
        ) from exc
    nodes = payload.get("nodes") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("run_key_name") != run_key_name
        or not isinstance(nodes, list)
    ):
        raise RuntimeError(
            f"Workspace ref cleanup found contradictory plan evidence: {plan_path}."
        )
    stage_paths: list[Path] = []
    for node in nodes:
        contract = node.get("artifact_contract") if isinstance(node, dict) else None
        stage_path = contract.get("stage_path") if isinstance(contract, dict) else None
        if not isinstance(stage_path, str) or not _safe_stage_path(stage_path):
            raise RuntimeError(
                f"Workspace ref cleanup found unsafe stage evidence: {plan_path}."
            )
        existing_path = _existing_planned_stage_path(run_dir, stage_path)
        if existing_path is not None:
            stage_paths.append(existing_path)
    return tuple(stage_paths)


def _existing_planned_stage_path(run_dir: Path, stage_path: str) -> Path | None:
    current = run_dir
    for part in Path(stage_path).parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RuntimeError(
                f"Workspace ref cleanup found unsafe stage evidence: {current}."
            ) from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise RuntimeError(
                f"Workspace ref cleanup found unsafe stage evidence: {current}."
            )
    return current


def _safe_stage_path(value: str) -> bool:
    path = Path(value)
    return (
        bool(value.strip())
        and not path.is_absolute()
        and all(part not in {"", ".", ".."} for part in path.parts)
        and path.parts[0] not in RESERVED_RUN_ROOT_NAMES
    )


def _is_ref_cleanup_evidence_name(name: str) -> bool:
    return name.endswith(".json") and (
        name == "workspace-state.json"
        or name.startswith("workspace-state-")
        or name.startswith("workspace-reuse-claim-")
        or _is_temporary_ref_evidence_name(name)
    )


def _is_temporary_ref_evidence_name(name: str) -> bool:
    return name.startswith("workspace-temporary-refs-") and name.endswith(".json")


def _require_temporary_ref_cleanup_evidence(
    payload: dict[str, object], evidence_path: Path
) -> None:
    git_payload = payload.get("git")
    claims = payload.get("temporary_refs")
    identity_fields = (
        "run_id",
        "run_key_name",
        "node_id",
        "task_id",
        "role",
    )
    if (
        any(not isinstance(payload.get(field), str) for field in identity_fields)
        or not isinstance(payload.get("round_num"), int)
        or not isinstance(git_payload, dict)
        or not isinstance(git_payload.get("repo_id"), str)
        or not isinstance(claims, list)
    ):
        raise RuntimeError(
            f"Workspace temporary ref cleanup evidence is invalid: {evidence_path}."
        )
    for claim in claims:
        if not _temporary_ref_claim_matches_owner(payload, git_payload, claim):
            raise RuntimeError(
                "Workspace temporary ref cleanup evidence is contradictory: "
                f"{evidence_path}."
            )


def _require_ref_cleanup_repository_identity(
    payload: dict[str, object],
    evidence_path: Path,
    expected_repository_id: str,
) -> None:
    git_payload = payload.get("git")
    claims = payload.get("temporary_refs")
    if (
        not isinstance(git_payload, dict)
        or git_payload.get("repo_id") != expected_repository_id
    ):
        raise RuntimeError(
            "Workspace ref cleanup evidence belongs to a different repository: "
            f"{evidence_path}."
        )
    if isinstance(claims, list) and any(
        not isinstance(claim, dict)
        or claim.get("repository_id") != expected_repository_id
        for claim in claims
    ):
        raise RuntimeError(
            "Workspace ref cleanup claim belongs to a different repository: "
            f"{evidence_path}."
        )


def _temporary_ref_claim_matches_owner(
    payload: dict[str, object],
    git_payload: dict[str, object],
    claim: object,
) -> bool:
    if not isinstance(claim, dict):
        return False
    return (
        claim.get("phase") in {"prepared", "removed"}
        and claim.get("owner_run_id") == payload.get("run_id")
        and claim.get("owner_node_id") == payload.get("node_id")
        and claim.get("owner_task_id") == payload.get("task_id")
        and claim.get("owner_role") == payload.get("role")
        and claim.get("owner_round_num") == payload.get("round_num")
        and claim.get("owner_audit_round_num") == payload.get("audit_round_num")
        and claim.get("repository_id") == git_payload.get("repo_id")
        and isinstance(claim.get("name"), str)
        and _is_object_id(claim.get("target_oid"))
    )


def _is_object_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )


def _require_drained_ref_cleanup_state(
    payload: dict[str, object], state_path: Path
) -> None:
    process_drain = payload.get("process_drain")
    workspace_mutator = payload.get("workspace_mutator")
    if (
        isinstance(process_drain, dict)
        and process_drain.get("status") == "unresolved"
        or isinstance(workspace_mutator, dict)
        and workspace_mutator.get("status") == "unresolved"
    ):
        raise RuntimeError(
            f"Workspace ref cleanup found unresolved mutator evidence: {state_path}."
        )


def cleanup_plan_workspace_refs(plan: PreflightExecutionPlan) -> int:
    source = plan.workspace_source
    if source is None:
        return 0
    repo_root = Path(source.git_top_level)
    project_root = repo_root / source.project_root_relative_path
    return delete_run_workspace_refs(
        repo_root,
        Path(source.common_git_dir),
        project_root,
        plan.run_key_name,
        (project_root / ".crewplane" / "execution-stages" / plan.run_key_name),
    )


def _current_repository_id(
    repo_root: Path,
    common_git_dir: Path,
    project_root: Path,
) -> str:
    actual_common_git_dir = resolve_common_git_dir(repo_root)
    if actual_common_git_dir != common_git_dir.resolve(strict=False):
        raise RuntimeError("Workspace ref cleanup repository identity changed.")
    object_format = git(repo_root).text("rev-parse", "--show-object-format=storage")
    return workspace_repository_id(
        actual_common_git_dir,
        project_root,
        object_format,
    )


def resolve_common_git_dir(repo_root: Path) -> Path:
    raw_path = git(repo_root).text("rev-parse", "--git-common-dir")
    common_git_dir = Path(raw_path)
    if not common_git_dir.is_absolute():
        common_git_dir = repo_root / common_git_dir
    return common_git_dir.resolve(strict=False)

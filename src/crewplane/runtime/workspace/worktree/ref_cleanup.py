from __future__ import annotations

import json
import shutil
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from os import scandir
from pathlib import Path
from typing import Never

from crewplane.artifacts.workspace.state.contracts import (
    require_workspace_state_contract,
)
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.workflow.keywords import RESERVED_RUN_ROOT_NAMES
from crewplane.core.workspace.git_policy import is_git_object_id
from crewplane.core.workspace.repository_identity import workspace_repository_id

from ..git import git
from ..state import read_workspace_state
from .ref_publication import reconcile_result_ref_publication
from .temporary_refs import reconcile_temporary_import_refs

WorkspaceRunRefCleanup = Callable[[str], int]


class _RefCleanupEvidenceKind(Enum):
    WORKSPACE_STATE = auto()
    TEMPORARY_REFS = auto()


@dataclass(frozen=True, slots=True)
class _RefCleanupContext:
    repo_root: Path
    common_git_dir: Path
    repository_id: str


@dataclass(frozen=True, slots=True)
class _RefCleanupEvidence:
    path: Path
    kind: _RefCleanupEvidenceKind


@dataclass(frozen=True, slots=True)
class _RunDirectoryScan:
    stage_paths: frozenset[Path]
    evidence_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _TemporaryRefEvidence:
    run_id: str
    run_key_name: str
    node_id: str
    task_id: str
    role: str
    round_num: int
    audit_round_num: object
    repository_id: str
    claims: tuple[object, ...]


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
    context = _RefCleanupContext(
        repo_root=repo_root,
        common_git_dir=common_git_dir,
        repository_id=_current_repository_id(
            repo_root,
            common_git_dir,
            project_root,
        ),
    )
    evidence = tuple(
        _load_ref_cleanup_evidence(path, run_key_name, context.repository_id)
        for path in _ref_cleanup_evidence_paths(run_dir, run_key_name)
    )
    return _reconcile_ref_cleanup_evidence(evidence, context)


def _load_ref_cleanup_evidence(
    evidence_path: Path,
    run_key_name: str,
    repository_id: str,
) -> _RefCleanupEvidence:
    if not evidence_path.is_file() or evidence_path.is_symlink():
        raise RuntimeError(
            f"Workspace ref cleanup found unsafe evidence: {evidence_path}."
        )
    payload = _read_ref_cleanup_evidence(evidence_path)
    if payload.get("run_key_name") != run_key_name:
        raise RuntimeError(
            f"Workspace ref cleanup found contradictory run evidence: {evidence_path}."
        )
    _require_ref_cleanup_repository_identity(payload, evidence_path, repository_id)
    if payload.get("evidence_kind") == "temporary_ref_cleanup":
        _require_temporary_ref_cleanup_evidence(payload, evidence_path)
        return _RefCleanupEvidence(
            evidence_path,
            _RefCleanupEvidenceKind.TEMPORARY_REFS,
        )
    require_workspace_state_contract(payload, "ref_cleanup")
    _require_drained_ref_cleanup_state(payload, evidence_path)
    return _RefCleanupEvidence(evidence_path, _RefCleanupEvidenceKind.WORKSPACE_STATE)


def _read_ref_cleanup_evidence(evidence_path: Path) -> dict[str, object]:
    try:
        return read_workspace_state(evidence_path)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Workspace ref cleanup found malformed evidence: {evidence_path}."
        ) from exc


def _reconcile_ref_cleanup_evidence(
    evidence: tuple[_RefCleanupEvidence, ...],
    context: _RefCleanupContext,
) -> int:
    removed = 0
    for record in evidence:
        removed += _reconcile_ref_cleanup_record(record, context)
    return removed


def _reconcile_ref_cleanup_record(
    evidence: _RefCleanupEvidence,
    context: _RefCleanupContext,
) -> int:
    removed = 0
    if evidence.kind is _RefCleanupEvidenceKind.WORKSPACE_STATE:
        removed += reconcile_result_ref_publication(
            evidence.path,
            context.repo_root,
            context.common_git_dir,
        )
    removed += reconcile_temporary_import_refs(
        evidence.path,
        context.repo_root,
        context.common_git_dir,
        context.repository_id,
    )
    if evidence.kind is _RefCleanupEvidenceKind.TEMPORARY_REFS:
        evidence.path.unlink()
    return removed


def _ref_cleanup_evidence_paths(
    run_dir: Path,
    run_key_name: str,
) -> tuple[Path, ...]:
    try:
        scan = _scan_run_directory(run_dir)
        stage_paths = set(scan.stage_paths)
        stage_paths.update(_planned_stage_paths(run_dir, run_key_name))
        paths = scan.evidence_paths + _stage_ref_cleanup_evidence_paths(stage_paths)
    except OSError as exc:
        raise RuntimeError(
            f"Workspace ref cleanup could not scan evidence beneath {run_dir}."
        ) from exc
    return tuple(sorted(paths))


def _scan_run_directory(run_dir: Path) -> _RunDirectoryScan:
    stage_paths: set[Path] = set()
    evidence_paths: list[Path] = []
    with scandir(run_dir) as entries:
        stage_entries = tuple(entries)
    for stage_entry in stage_entries:
        if stage_entry.name in RESERVED_RUN_ROOT_NAMES:
            if stage_entry.name == "logs":
                evidence_paths.extend(
                    _run_log_temporary_ref_evidence(Path(stage_entry.path))
                )
            continue
        stage_path = Path(stage_entry.path)
        if stage_entry.is_symlink():
            raise RuntimeError(
                f"Workspace ref cleanup found unsafe evidence: {stage_path}."
            )
        if stage_entry.is_dir(follow_symlinks=False):
            stage_paths.add(stage_path)
    return _RunDirectoryScan(frozenset(stage_paths), tuple(evidence_paths))


def _stage_ref_cleanup_evidence_paths(stage_paths: set[Path]) -> tuple[Path, ...]:
    evidence_paths: list[Path] = []
    for stage_path in sorted(stage_paths):
        with scandir(stage_path) as entries:
            evidence_paths.extend(
                Path(entry.path)
                for entry in entries
                if _is_ref_cleanup_evidence_name(entry.name)
            )
    return tuple(evidence_paths)


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
    plan_path = _cleanup_plan_path(run_dir)
    if plan_path is None:
        return ()
    payload = _read_cleanup_plan(plan_path)
    stage_path_values = _stage_path_values_from_plan(
        payload,
        plan_path,
        run_key_name,
    )
    return _existing_planned_stage_paths(run_dir, stage_path_values)


def _cleanup_plan_path(run_dir: Path) -> Path | None:
    preflight_path = run_dir / "preflight"
    plan_path = preflight_path / "execution-plan.json"
    try:
        preflight_mode = preflight_path.lstat().st_mode
        plan_stat = plan_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(
            f"Workspace ref cleanup could not read the plan beneath {run_dir}."
        ) from exc
    if not _safe_plan_evidence(
        preflight_mode,
        plan_stat.st_mode,
        plan_stat.st_nlink,
    ):
        raise RuntimeError(
            f"Workspace ref cleanup found unsafe plan evidence: {plan_path}."
        )
    return plan_path


def _safe_plan_evidence(
    preflight_mode: int,
    plan_mode: int,
    plan_link_count: int,
) -> bool:
    return not (
        stat.S_ISLNK(preflight_mode)
        or not stat.S_ISDIR(preflight_mode)
        or stat.S_ISLNK(plan_mode)
        or not stat.S_ISREG(plan_mode)
        or plan_link_count != 1
    )


def _read_cleanup_plan(plan_path: Path) -> object:
    try:
        return json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"Workspace ref cleanup found malformed plan evidence: {plan_path}."
        ) from exc


def _stage_path_values_from_plan(
    payload: object,
    plan_path: Path,
    run_key_name: str,
) -> tuple[str, ...]:
    nodes = payload.get("nodes") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("run_key_name") != run_key_name
        or not isinstance(nodes, list)
    ):
        raise RuntimeError(
            f"Workspace ref cleanup found contradictory plan evidence: {plan_path}."
        )
    return tuple(_stage_path_value(node, plan_path) for node in nodes)


def _stage_path_value(node: object, plan_path: Path) -> str:
    contract = node.get("artifact_contract") if isinstance(node, dict) else None
    stage_path = contract.get("stage_path") if isinstance(contract, dict) else None
    if not isinstance(stage_path, str) or not _safe_stage_path(stage_path):
        raise RuntimeError(
            f"Workspace ref cleanup found unsafe stage evidence: {plan_path}."
        )
    return stage_path


def _existing_planned_stage_paths(
    run_dir: Path,
    stage_path_values: tuple[str, ...],
) -> tuple[Path, ...]:
    stage_paths: list[Path] = []
    for stage_path in stage_path_values:
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
    evidence = _temporary_ref_evidence(payload, evidence_path)
    if any(
        not _temporary_ref_claim_matches_owner(evidence, claim)
        for claim in evidence.claims
    ):
        raise RuntimeError(
            "Workspace temporary ref cleanup evidence is contradictory: "
            f"{evidence_path}."
        )


def _temporary_ref_evidence(
    payload: dict[str, object],
    evidence_path: Path,
) -> _TemporaryRefEvidence:
    git_payload = payload.get("git")
    claims = payload.get("temporary_refs")
    round_num = payload.get("round_num")
    if (
        not isinstance(round_num, int)
        or not isinstance(git_payload, dict)
        or not isinstance(claims, list)
    ):
        _raise_invalid_temporary_ref_evidence(evidence_path)
    return _TemporaryRefEvidence(
        run_id=_temporary_ref_string(payload, "run_id", evidence_path),
        run_key_name=_temporary_ref_string(payload, "run_key_name", evidence_path),
        node_id=_temporary_ref_string(payload, "node_id", evidence_path),
        task_id=_temporary_ref_string(payload, "task_id", evidence_path),
        role=_temporary_ref_string(payload, "role", evidence_path),
        round_num=round_num,
        audit_round_num=payload.get("audit_round_num"),
        repository_id=_temporary_ref_string(git_payload, "repo_id", evidence_path),
        claims=tuple(claims),
    )


def _temporary_ref_string(
    payload: dict[str, object],
    field_name: str,
    evidence_path: Path,
) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str):
        _raise_invalid_temporary_ref_evidence(evidence_path)
    return value


def _raise_invalid_temporary_ref_evidence(evidence_path: Path) -> Never:
    raise RuntimeError(
        f"Workspace temporary ref cleanup evidence is invalid: {evidence_path}."
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
    evidence: _TemporaryRefEvidence,
    claim: object,
) -> bool:
    if not isinstance(claim, dict):
        return False
    return (
        claim.get("phase") in {"prepared", "removed"}
        and claim.get("owner_run_id") == evidence.run_id
        and claim.get("owner_node_id") == evidence.node_id
        and claim.get("owner_task_id") == evidence.task_id
        and claim.get("owner_role") == evidence.role
        and claim.get("owner_round_num") == evidence.round_num
        and claim.get("owner_audit_round_num") == evidence.audit_round_num
        and claim.get("repository_id") == evidence.repository_id
        and isinstance(claim.get("name"), str)
        and is_git_object_id(claim.get("target_oid"))
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

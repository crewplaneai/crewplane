from __future__ import annotations

import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from crewplane.artifacts.workspace.state.paths import (
    is_safe_workspace_stage_path,
    is_temporary_ref_evidence_name,
    is_workspace_claim_name,
)
from crewplane.core.preflight.models import PreflightExecutionPlan


@dataclass(frozen=True)
class PlannedStageDirectories:
    directories: tuple[tuple[Path, str], ...]
    invalid: bool


@dataclass(frozen=True)
class EvidenceDirectoryScan:
    paths: tuple[Path, ...]
    invalid: bool


@dataclass(frozen=True)
class ClaimPath:
    state_path: Path
    expected_node_id: str


@dataclass(frozen=True)
class ClaimPathScan:
    claims: tuple[ClaimPath, ...]
    invalid: bool


def planned_stage_directories(
    run_dir: Path,
    plan: PreflightExecutionPlan,
) -> PlannedStageDirectories:
    directories: list[tuple[Path, str]] = []
    for node in plan.nodes:
        stage_path = node.artifact_contract.stage_path
        if stage_path is None or not is_safe_workspace_stage_path(stage_path):
            return PlannedStageDirectories((), True)
        directories.append((run_dir / stage_path, node.id))
    return PlannedStageDirectories(tuple(directories), False)


def scan_dedicated_ref_evidence(
    run_dir: Path,
    stage_dirs: tuple[tuple[Path, str], ...],
) -> EvidenceDirectoryScan:
    evidence_dirs = (run_dir / "logs", *(path for path, _node_id in stage_dirs))
    found: list[Path] = []
    invalid = False
    for evidence_dir in evidence_dirs:
        scan = _scan_directory(evidence_dir, run_dir, is_temporary_ref_evidence_name)
        found.extend(scan.paths)
        invalid = invalid or scan.invalid
    return EvidenceDirectoryScan(tuple(found), invalid)


def scan_claim_paths(
    run_dir: Path,
    stage_dirs: tuple[tuple[Path, str], ...],
) -> ClaimPathScan:
    candidates: list[ClaimPath] = []
    invalid = False
    for stage_dir, expected_node_id in stage_dirs:
        scan = _scan_claim_directory(stage_dir, run_dir)
        candidates.extend(ClaimPath(path, expected_node_id) for path in scan.paths)
        invalid = invalid or scan.invalid
    return ClaimPathScan(
        tuple(sorted(candidates, key=lambda claim: claim.state_path.as_posix())),
        invalid,
    )


def single_link_regular_file(path: Path) -> bool:
    try:
        file_stat = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(file_stat.st_mode) and file_stat.st_nlink == 1


def path_has_symlink_component(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if stat.S_ISLNK(mode):
            return True
    return False


def _scan_directory(
    directory: Path,
    run_dir: Path,
    file_name_matches: Callable[[str], bool],
) -> EvidenceDirectoryScan:
    if not directory.exists():
        return EvidenceDirectoryScan((), False)
    if path_has_symlink_component(directory, run_dir) or not directory.is_dir():
        return EvidenceDirectoryScan((), True)
    try:
        entries = tuple(directory.iterdir())
    except OSError:
        return EvidenceDirectoryScan((), True)
    matches = tuple(path for path in entries if file_name_matches(path.name))
    if any(not single_link_regular_file(path) for path in matches):
        return EvidenceDirectoryScan((), True)
    return EvidenceDirectoryScan(matches, False)


def _scan_claim_directory(stage_dir: Path, run_dir: Path) -> EvidenceDirectoryScan:
    if path_has_symlink_component(stage_dir, run_dir):
        return EvidenceDirectoryScan((), True)
    if not stage_dir.is_dir():
        return EvidenceDirectoryScan((), False)
    try:
        entries = tuple(stage_dir.iterdir())
    except OSError:
        return EvidenceDirectoryScan((), True)
    candidates = tuple(path for path in entries if is_workspace_claim_name(path.name))
    safe = tuple(path for path in candidates if single_link_regular_file(path))
    return EvidenceDirectoryScan(safe, len(safe) != len(candidates))

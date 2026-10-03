from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from crewplane.artifacts.atomic import atomic_write_json
from crewplane.artifacts.generated_files.evidence import (
    verified_generated_file_descriptors,
)
from crewplane.artifacts.generated_files.paths import (
    RESERVED_WORKSPACE_PATH_ROOTS,
)
from crewplane.core.preflight.workspace.models import is_lineage_worktree
from crewplane.runtime.execution.activity.telemetry import ActivityTrackerSnapshot
from crewplane.runtime.workspace.git import git
from crewplane.runtime.workspace.invocation import invocation_slug, workspace_state_path
from crewplane.runtime.workspace.snapshot_scan import (
    WorkspaceSnapshotError,
    WorkspaceSnapshotPolicy,
    snapshot_entries,
)
from crewplane.runtime.workspace.worktree.descriptors import load_source_ref_from_state

if TYPE_CHECKING:
    from .types import ExecutorRoundArtifact, ExecutorRoundRequest


@dataclass(frozen=True)
class CandidateIdentity:
    kind: Literal["document", "files", "unverified"]
    fingerprint: str | None
    source_fingerprint: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ProjectObservation:
    fingerprint: str | None
    activity: ActivityTrackerSnapshot | None


def content_fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


async def observe_project(request: ExecutorRoundRequest) -> ProjectObservation:
    activity = _activity_snapshot(request)
    policy = request.node.workspace_policy
    if policy is not None and policy.enabled:
        return ProjectObservation(None, activity)
    fingerprint = await asyncio.to_thread(
        project_fingerprint,
        Path(request.runtime_context.plan.project_root),
        (request.output.stages_dir, request.output.results_dir),
    )
    if activity != _activity_snapshot(request):
        fingerprint = None
    return ProjectObservation(fingerprint, activity)


async def bind_candidate_identities(
    request: ExecutorRoundRequest,
    outputs: list[ExecutorRoundArtifact],
    before: ProjectObservation,
) -> list[ExecutorRoundArtifact]:
    after = await observe_project(request)
    bound_outputs = []
    for artifact in outputs:
        identity = await asyncio.to_thread(
            _candidate_identity, request, artifact, before, after
        )
        atomic_write_json(
            artifact.output_file.with_suffix(".candidate.json"), asdict(identity)
        )
        bound_outputs.append(replace(artifact, candidate_identity=identity))
    return bound_outputs


def project_fingerprint(root: Path, artifact_roots: tuple[Path, ...]) -> str | None:
    """Observe bounded project contents without using invocation-local state."""
    root = root.resolve()
    excluded = set(RESERVED_WORKSPACE_PATH_ROOTS) | {".venv"}
    for artifact_root in artifact_roots:
        if artifact_root.is_relative_to(root):
            excluded.add(artifact_root.relative_to(root).as_posix())
    try:
        entries = snapshot_entries(
            root,
            WorkspaceSnapshotPolicy(
                max_entries=25_000,
                max_file_bytes=128 * 1024 * 1024,
                max_elapsed_seconds=5.0,
                excluded_roots=frozenset(excluded),
            ),
        )
    except (OSError, WorkspaceSnapshotError):
        return None
    return content_fingerprint(entries)


def _activity_snapshot(request: ExecutorRoundRequest) -> ActivityTrackerSnapshot | None:
    if request.telemetry is not None and request.telemetry.activity_tracker is not None:
        return request.telemetry.activity_tracker.snapshot(request.node.id)
    return None


def _project_observation_is_reliable(
    request: ExecutorRoundRequest,
    before: ProjectObservation,
    after: ProjectObservation,
) -> bool:
    if before.fingerprint is None or after.fingerprint is None:
        return False
    if before.activity is not None:
        return before.activity.is_exclusive and before.activity == after.activity
    return (
        request.runtime_context.max_concurrent_nodes() == 1
        or len(request.runtime_context.plan.execution_order) == 1
    )


def _candidate_identity(
    request: ExecutorRoundRequest,
    artifact: ExecutorRoundArtifact,
    before: ProjectObservation,
    after: ProjectObservation,
) -> CandidateIdentity:
    generated = _generated_file_descriptors(request, artifact)
    if generated is None:
        return CandidateIdentity(
            "unverified", None, reason="generated_files_unavailable"
        )
    if is_lineage_worktree(request.node.workspace_policy):
        return _worktree_identity(request, artifact, generated)
    policy = request.node.workspace_policy
    if policy is not None and policy.enabled:
        return CandidateIdentity(
            "files" if generated else "document",
            content_fingerprint(generated or artifact.content),
        )
    return _project_candidate_identity(request, artifact, generated, before, after)


def _project_candidate_identity(
    request: ExecutorRoundRequest,
    artifact: ExecutorRoundArtifact,
    generated: list[tuple[str, int, str]],
    before: ProjectObservation,
    after: ProjectObservation,
) -> CandidateIdentity:
    if not _project_observation_is_reliable(request, before, after):
        return CandidateIdentity(
            "unverified", None, reason="project_changes_unattributable"
        )
    file_backed = _is_file_candidate(
        request, generated, before.fingerprint != after.fingerprint
    )
    return CandidateIdentity(
        "files" if file_backed else "document",
        content_fingerprint(
            [after.fingerprint, None if file_backed else artifact.content]
        ),
        source_fingerprint=after.fingerprint,
    )


def _is_file_candidate(
    request: ExecutorRoundRequest,
    generated: list[tuple[str, int, str]],
    project_changed: bool,
) -> bool:
    return len(request.executors) == 1 and (
        bool(generated)
        or project_changed
        or any(
            previous.candidate_identity is not None
            and previous.candidate_identity.kind == "files"
            for previous in request.previous_executor_outputs or []
        )
    )


def _worktree_identity(
    request: ExecutorRoundRequest,
    artifact: ExecutorRoundArtifact,
    generated: list[tuple[str, int, str]],
) -> CandidateIdentity:
    slug = invocation_slug(
        request.node.id, artifact.task_id, request.audit_round_num, request.round_num
    )
    state_path = workspace_state_path(
        request.output, request.node, slug, request.audit_round_num, request.round_num
    )
    try:
        source = load_source_ref_from_state(state_path)
        generated = _generated_files_outside_tree(
            request, source.source_tree, generated
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return CandidateIdentity(
            "unverified", None, reason="workspace_result_unavailable"
        )
    return CandidateIdentity(
        "files",
        content_fingerprint([source.source_tree, generated]),
        source_fingerprint=source.source_tree,
    )


def _generated_files_outside_tree(
    request: ExecutorRoundRequest,
    tree: str,
    generated: list[tuple[str, int, str]],
) -> list[tuple[str, int, str]]:
    if not generated:
        return []
    workspace_source = request.runtime_context.plan.workspace_source
    if workspace_source is None:
        raise RuntimeError("Workspace source is required to compare generated files.")
    paths_in_tree = set(
        git(Path(workspace_source.git_top_level), timeout_seconds=5.0).zero_records(
            "ls-tree", "-r", "--name-only", "-z", tree
        )
    )
    prefix = Path(workspace_source.project_root_relative_path)
    return [
        descriptor
        for descriptor in generated
        if (prefix / descriptor[0]).as_posix() not in paths_in_tree
    ]


def _generated_file_descriptors(
    request: ExecutorRoundRequest,
    artifact: ExecutorRoundArtifact,
) -> list[tuple[str, int, str]] | None:
    """Return verified descriptors, or None when capture evidence is unavailable."""
    roots = request.runtime_context.generated_file_workspaces.roots_for_node(
        request.node.id
    )
    key = artifact.output_file.resolve()
    if key not in roots:
        return []
    root = roots[key]
    if root is None:
        return None
    return verified_generated_file_descriptors(root)

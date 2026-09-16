from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from crewplane.architecture.safe_files import contained_regular_file
from crewplane.artifacts.atomic import atomic_write_json
from crewplane.artifacts.generated_files.paths import (
    GENERATED_FILE_SNAPSHOT_METADATA_NAME,
    RESERVED_WORKSPACE_PATH_ROOTS,
)
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.workspace.models import is_lineage_worktree
from crewplane.runtime.execution.activity.telemetry import ActivityTrackerSnapshot
from crewplane.runtime.workspace.git import git
from crewplane.runtime.workspace.invocation import invocation_slug, workspace_state_path
from crewplane.runtime.workspace.snapshot import (
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
    fingerprint = await asyncio.to_thread(_project_fingerprint, request)
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


def _project_fingerprint(request: ExecutorRoundRequest) -> str | None:
    root = Path(request.runtime_context.plan.project_root).resolve()
    excluded = set(RESERVED_WORKSPACE_PATH_ROOTS) | {".venv"}
    for artifact_root in (request.output.stages_dir, request.output.results_dir):
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
    if not _project_observation_is_reliable(request, before, after):
        return CandidateIdentity(
            "unverified", None, reason="project_changes_unattributable"
        )
    file_backed = len(request.executors) == 1 and (
        bool(generated)
        or before.fingerprint != after.fingerprint
        or any(
            previous.candidate_identity is not None
            and previous.candidate_identity.kind == "files"
            for previous in request.previous_executor_outputs or []
        )
    )
    return CandidateIdentity(
        "files" if file_backed else "document",
        content_fingerprint(
            [after.fingerprint, None if file_backed else artifact.content]
        ),
        source_fingerprint=after.fingerprint,
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
    roots = request.runtime_context.generated_file_workspaces.roots_for_node(
        request.node.id
    )
    key = artifact.output_file.resolve()
    if key not in roots:
        return []
    root = roots[key]
    if root is None:
        return None
    metadata = contained_regular_file(root, GENERATED_FILE_SNAPSHOT_METADATA_NAME)
    if metadata is None:
        return None
    try:
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("rejected_file_count", 0):
            return None
        files = payload.get("files")
        if not isinstance(files, list):
            return None
        descriptors = []
        for entry in files:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                return None
            path = contained_regular_file(root, entry["path"])
            if path is None:
                return None
            size, digest = file_size_and_sha256(path)
            if size != entry.get("size_bytes"):
                return None
            descriptors.append((entry["path"], size, digest))
    except (OSError, ValueError):
        return None
    return sorted(descriptors)

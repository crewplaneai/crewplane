"""Immutable semantic workspace evidence for unfinished review nodes."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from crewplane.architecture.ports.artifacts import ArtifactStorePort
from crewplane.architecture.safe_files import (
    contained_regular_file,
    ensure_contained_directory,
)
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
)
from crewplane.core.review_checkpoint import OpenReviewCheckpoint, ReviewLoopCheckpoint
from crewplane.core.review_checkpoint_state import (
    CheckpointFile,
    CheckpointInvocation,
    CheckpointProgress,
    CheckpointWorkspace,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workspace.invocation_identity import invocation_slug

from ..atomic import atomic_write_bytes_if_absent, json_bytes
from ..resume.checkpoint_files import describe_checkpoint_file, read_checkpoint_file
from .state.contracts import require_workspace_state_contract
from .state.fields import WorkspaceEvidenceRoot, mapping_value
from .state.paths import workspace_bundle_path, workspace_state_filename
from .state.validation import checkpoint_invocation_is_valid


@dataclass(frozen=True)
class PreparedCheckpointWorkspaces:
    """Workspace references, dependency descriptors, and unpublished snapshot bytes.

    Preparation orders workspaces and snapshots by destination, and files by
    first-seen path with the last descriptor winning. Fields cannot be rebound;
    their collections remain mutable and are independently allocated by default.
    """

    workspaces: list[CheckpointWorkspace] = field(default_factory=list)
    files: list[CheckpointFile] = field(default_factory=list)
    snapshots: dict[str, bytes] = field(default_factory=dict)


def semantic_workspace_state(payload: dict[str, object]) -> dict[str, object]:
    """Validate evidence and return a deep copy stripped of physical/ref ownership.

    Require the failed-invocation contract for failed state, otherwise the resume
    contract. Clear execution placement and mark checkpoint retention without
    mutating the input or writing files. Contract errors propagate as RuntimeError.
    """
    require_workspace_state_contract(
        payload, "failed_invocation" if payload.get("status") == "failed" else "resume"
    )
    result = deepcopy(payload)
    for key in (
        "temporary_refs",
        "ref_publication",
        "branch_export",
        "resume_origin",
        "reuse",
    ):
        result.pop(key, None)
    workspace = mapping_value(result.get("workspace"))
    for key in (
        "path",
        "effective_cwd",
        "cache_root",
        "checkout_root",
        "cache_key",
        "reuse_generation",
    ):
        workspace.pop(key, None)
    workspace.update(retention="not_applicable", retained_reason="checkpoint")
    result["workspace"] = workspace
    result["execution"] = {}
    return result


def checkpoint_invocations(progress: CheckpointProgress) -> list[CheckpointInvocation]:
    """Collect candidate producers, then reviews and failures, without mutation.

    Use producer coordinates for candidates and invocation coordinates otherwise.
    Deduplicate by task, audit, and round in first-seen order; the last record wins.
    Invalid invocation coordinates propagate as pydantic.ValidationError.
    """
    invocations = [
        CheckpointInvocation(
            task_id=item.task_id,
            role=item.role,
            audit=item.producer_audit,
            local_round=item.producer_round,
        )
        for item in progress.candidates()
    ]
    invocations.extend(
        CheckpointInvocation(
            task_id=item.task_id,
            role=item.role,
            audit=item.audit,
            local_round=item.local_round,
        )
        for item in [*progress.reviews(), *progress.failures()]
    )
    return list(
        {
            (item.task_id, item.audit, item.local_round): item for item in invocations
        }.values()
    )


def invocation_destination(
    node: PreflightExecutionNode, invocation: CheckpointInvocation
) -> str:
    """Return the invocation's workspace-state path relative to the stage root.

    Include the audit in the slug only when the node has multiple audit rounds.
    This performs no I/O and does not modify the node or invocation.
    """
    audit = invocation.audit if (node.execution_policy.audit_rounds or 1) > 1 else None
    slug = invocation_slug(node.id, invocation.task_id, audit, invocation.local_round)
    return f"{node.artifact_contract.stage_path}/{workspace_state_filename(slug)}"


def _invocation_from_payload(payload: dict[str, object]) -> CheckpointInvocation:
    return CheckpointInvocation.model_validate(
        {
            "task_id": payload.get("task_id"),
            "role": payload.get("role"),
            "audit": payload.get("audit_round_num") or 1,
            "local_round": payload.get("round_num"),
        }
    )


def _ancestor_destinations(
    payload: dict[str, object], node: PreflightExecutionNode
) -> list[str]:
    source = mapping_value(payload.get("source"))
    destinations = []
    bundle_destinations = _executor_bundle_destinations(node)
    while source.get("node_id") == node.id:
        bundle = source.get("bundle_path")
        if not isinstance(bundle, str) or bundle not in bundle_destinations:
            raise ValueError(
                "Checkpoint workspace ancestor has no matching invocation bundle."
            )
        destinations.append(bundle_destinations[bundle])
        upstreams = source.get("upstream_sources")
        if not isinstance(upstreams, list) or len(upstreams) != 1:
            raise ValueError("Checkpoint workspace source chain is incomplete.")
        source = mapping_value(upstreams[0])
    return destinations


def _executor_bundle_destinations(node: PreflightExecutionNode) -> dict[str, str]:
    """Resolve bounded bundle names without scanning uncommitted invocation files."""
    destinations = {}
    audit_rounds = node.execution_policy.audit_rounds or 1
    for provider in node.provider_records:
        if provider.role != ProviderRole.EXECUTOR:
            continue
        for audit in range(1, audit_rounds + 1):
            for local_round in range(1, (node.execution_policy.depth or 1) + 2):
                slug = invocation_slug(
                    node.id,
                    provider.task_id,
                    audit if audit_rounds > 1 else None,
                    local_round,
                )
                destination = Path(
                    node.artifact_contract.stage_path or ""
                ) / workspace_state_filename(slug)
                destinations[workspace_bundle_path(destination, slug).as_posix()] = (
                    destination.as_posix()
                )
    return destinations


def prepare_checkpoint_workspaces(
    output: ArtifactStorePort,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    progress: CheckpointProgress,
) -> PreparedCheckpointWorkspaces:
    """Prepare descriptor-backed semantic snapshots without publishing them.

    Disabled workspaces return empty collections without I/O. Otherwise load
    invocations and their same-node ancestors in stack order, preferring snapshots
    in the previous open checkpoint over live state. Validate all loaded payloads
    before assembling snapshots in destination order, with each state's descriptor
    followed by its bundle and setup metadata/log descriptors. Deduplicate files
    in first-seen path order, retaining the last descriptor. Inputs stay unchanged.

    Read, JSON decoding, contract, invocation, and dependency errors propagate at
    the first failure. Semantic validation owns any temporary Git resources.
    """
    policy = node.workspace_policy
    if policy is None or not policy.enabled:
        return PreparedCheckpointWorkspaces()
    root = output.stages_dir
    previous = output.read_review_checkpoint(node.id)
    payloads = _load_workspace_payloads(root, node, progress, previous)
    _validate_payloads(root, plan, node, payloads, output.run_id, output.run_key_name)
    return _prepare_workspace_snapshots(root, node, payloads)


def _load_workspace_payloads(
    root: Path,
    node: PreflightExecutionNode,
    progress: CheckpointProgress,
    previous: ReviewLoopCheckpoint | None,
) -> dict[str, dict[str, object]]:
    carried = (
        {item.destination_path: item for item in previous.workspaces}
        if isinstance(previous, OpenReviewCheckpoint)
        else {}
    )
    descriptors = (
        {item.relative_path: item for item in previous.files}
        if isinstance(previous, OpenReviewCheckpoint)
        else {}
    )
    pending = [
        invocation_destination(node, item) for item in checkpoint_invocations(progress)
    ]
    payloads: dict[str, dict[str, object]] = {}
    while pending:
        destination = pending.pop()
        if destination in payloads:
            continue
        snapshot = carried.get(destination)
        if snapshot is not None:
            payload = json.loads(
                read_checkpoint_file(root, descriptors[snapshot.snapshot_path])
            )
        else:
            path = contained_regular_file(root, destination)
            if path is None:
                raise ValueError(
                    f"Checkpoint workspace evidence is missing: {destination}"
                )
            payload = semantic_workspace_state(json.loads(path.read_bytes()))
        if not isinstance(payload, dict):
            raise ValueError("Checkpoint workspace metadata must be an object.")
        payloads[destination] = payload
        pending.extend(_ancestor_destinations(payload, node))
    return payloads


def _prepare_workspace_snapshots(
    root: Path,
    node: PreflightExecutionNode,
    payloads: dict[str, dict[str, object]],
) -> PreparedCheckpointWorkspaces:
    workspaces, files = [], []
    snapshots = {}
    for destination, payload in sorted(payloads.items()):
        invocation = _invocation_from_payload(payload)
        encoded = json_bytes(payload)
        digest = hashlib.sha256(encoded).hexdigest()
        name = f"{Path(destination).stem.removeprefix('workspace-state-')}--{digest[:16]}.json"
        relative = f"{node.artifact_contract.stage_path}/review-state/checkpoints/workspaces/{name}"
        snapshots[relative] = encoded
        files.append(
            CheckpointFile(
                **invocation.model_dump(),
                relative_path=relative,
                purpose="workspace_state",
                signature=(len(encoded), digest),
            )
        )
        workspaces.append(
            CheckpointWorkspace(
                **invocation.model_dump(),
                snapshot_path=relative,
                destination_path=destination,
            )
        )
        files.extend(
            _workspace_dependencies(root, destination, payload, invocation, node)
        )
    return PreparedCheckpointWorkspaces(
        workspaces,
        list({item.relative_path: item for item in files}.values()),
        snapshots,
    )


def publish_checkpoint_workspaces(
    root: Path, prepared: PreparedCheckpointWorkspaces
) -> None:
    """Publish snapshots in mapping order and verify each against its descriptor.

    Create contained directories and atomically write absent snapshots; verify
    existing files without replacing them. Stop on the first containment,
    signature, descriptor lookup, or I/O error. Earlier files and directories
    remain published on failure; the atomic writer owns temporary-file cleanup.
    Concurrent creation errors propagate. Prepared collections stay unchanged.
    """
    descriptors = {item.relative_path: item for item in prepared.files}
    for relative, encoded in prepared.snapshots.items():
        destination = Path(relative)
        directory = ensure_contained_directory(root, destination.parent.as_posix())
        path = directory / destination.name
        if not path.exists():
            atomic_write_bytes_if_absent(path, encoded)
        read_checkpoint_file(root, descriptors[relative])


def _workspace_dependencies(
    root: Path,
    destination: str,
    payload: dict[str, object],
    invocation: CheckpointInvocation,
    node: PreflightExecutionNode,
) -> list[CheckpointFile]:
    files = []
    bundle = mapping_value(payload.get("bundle"))
    if bundle:
        path = bundle.get("path")
        if not isinstance(path, str):
            raise ValueError("Workspace bundle lacks its path.")
        files.append(
            describe_checkpoint_file(root, path, invocation, "workspace_bundle")
        )
    policy = node.workspace_policy
    if policy is not None and policy.setup is not None:
        setup = mapping_value(payload.get("setup"))
        for key in ("metadata_path", "log_path"):
            value = setup.get(key)
            if not isinstance(value, str):
                raise ValueError("Checkpoint workspace lacks setup evidence.")
            path = f"{Path(destination).parent.as_posix()}/{value}"
            files.append(
                describe_checkpoint_file(root, path, invocation, "workspace_setup")
            )
    return files


def _validate_payloads(
    root: Path,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payloads: dict[str, dict[str, object]],
    run_id: str,
    run_key_name: str,
) -> None:
    # source_validation imports state, whose __init__ loads validation back into
    # source_validation; defer until both modules have finished initializing.
    from .source_validation import checkpoint_invocation_source_matches

    source = WorkspaceEvidenceRoot(root)
    carried = tuple(payloads.values())
    providers = {provider.task_id: provider for provider in node.provider_records}
    for destination, payload in payloads.items():
        invocation = _invocation_from_payload(payload)
        provider = providers.get(invocation.task_id)
        if (
            provider is None
            or payload.get("provider") != provider.provider
            or payload.get("role") != provider.role
            or destination != invocation_destination(node, invocation)
        ):
            raise ValueError("Checkpoint workspace invocation identity mismatch.")
        source_matches = checkpoint_invocation_source_matches(
            source, plan, node, payload, carried
        )
        if not checkpoint_invocation_is_valid(
            source, plan, node, payload, source_matches, run_id, run_key_name
        ):
            raise ValueError(f"Invalid checkpoint workspace invocation: {destination}")


def validate_checkpoint_workspaces(
    root: Path,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    checkpoint: OpenReviewCheckpoint,
) -> set[str]:
    """Validate carried workspace closure and return its bound dependency paths.

    Disabled workspaces reject carried state or return an empty set without I/O.
    For managed workspaces, read snapshots in checkpoint order, check coordinates,
    then describe and match bundle/setup dependencies. Check invocation closure
    before semantic validation in payload insertion order. Inputs stay unchanged;
    semantic validation owns any temporary Git resources.

    ValueError reports invalid evidence, dependencies, or closure. Descriptor
    lookup, decoding, model validation, and I/O errors propagate unchanged.
    """
    policy = node.workspace_policy
    if policy is None or not policy.enabled:
        if checkpoint.workspaces:
            raise ValueError(
                "Unmanaged checkpoint cannot carry managed workspace state."
            )
        return set()
    paths: set[str] = set()
    files = {item.relative_path: item for item in checkpoint.files}
    payloads: dict[str, dict[str, object]] = {}
    for workspace in checkpoint.workspaces:
        payload, expected = _validate_workspace_entry(root, node, workspace, files)
        payloads[workspace.destination_path] = payload
        paths.add(workspace.snapshot_path)
        paths.update(item.relative_path for item in expected)
    expected_destinations = {
        invocation_destination(node, item)
        for item in checkpoint_invocations(checkpoint.progress)
    }
    for payload in payloads.values():
        expected_destinations.update(_ancestor_destinations(payload, node))
    if set(payloads) != expected_destinations:
        raise ValueError("Checkpoint workspace invocation set is incomplete.")
    _validate_payloads(
        root, plan, node, payloads, checkpoint.run_id, checkpoint.run_key_name
    )
    return paths


def _validate_workspace_entry(
    root: Path,
    node: PreflightExecutionNode,
    workspace: CheckpointWorkspace,
    files: dict[str, CheckpointFile],
) -> tuple[dict[str, object], list[CheckpointFile]]:
    payload = json.loads(read_checkpoint_file(root, files[workspace.snapshot_path]))
    if not isinstance(payload, dict):
        raise ValueError("Invalid checkpoint workspace metadata.")
    if _invocation_from_payload(payload) != CheckpointInvocation(
        task_id=workspace.task_id,
        role=workspace.role,
        audit=workspace.audit,
        local_round=workspace.local_round,
    ):
        raise ValueError("Checkpoint workspace coordinates mismatch.")
    expected = _workspace_dependencies(
        root, workspace.destination_path, payload, workspace, node
    )
    if any(files.get(item.relative_path) != item for item in expected):
        raise ValueError("Workspace dependency is not descriptor-backed.")
    return payload, expected

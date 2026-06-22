from __future__ import annotations

import json
from pathlib import Path

from orchestrator_cli.core.preflight.models import PreflightExecutionPlan

from .worktree_types import WorktreeCaptureResult, WorktreeSourceRef


def load_source_ref_from_state(path: Path) -> WorktreeSourceRef:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("status") != "succeeded":
        raise RuntimeError(
            f"Workspace lineage source is not a succeeded state: {path.as_posix()}"
        )
    workspace = payload.get("workspace")
    if (
        not isinstance(workspace, dict)
        or workspace.get("lineage_producer") is not True
        or payload.get("role") != "executor"
    ):
        raise RuntimeError(
            f"Workspace lineage source is not an executor lineage state: {path.as_posix()}"
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(
            f"Workspace lineage source is missing result metadata: {path.as_posix()}"
        )
    result_commit = _string(result.get("result_commit"))
    result_tree = _string(result.get("result_tree"))
    if result_commit is None or result_tree is None:
        raise RuntimeError(
            f"Workspace lineage source has incomplete result metadata: {path.as_posix()}"
        )
    bundle_path, bundle_sha256, bundle_size_bytes = _bundle_descriptor(path, payload)
    refs = payload.get("refs")
    bundle_ref = _string(refs.get("result")) if isinstance(refs, dict) else None
    source_node_id = payload.get("node_id")
    return WorktreeSourceRef(
        source_kind="node",
        source_node_id=_string(source_node_id),
        source_commit=result_commit,
        source_tree=result_tree,
        candidate_sequence=1,
        bundle_path=bundle_path,
        bundle_sha256=bundle_sha256,
        bundle_size_bytes=bundle_size_bytes,
        bundle_ref=bundle_ref,
        upstream_sources=_upstream_sources(path, payload),
    )


def bundle_descriptor(
    plan: PreflightExecutionPlan,
    result: WorktreeCaptureResult,
    state_path: Path | None = None,
) -> dict[str, object]:
    bundle_path = result.bundle_path
    relative_path = relative_bundle_path(plan, bundle_path, state_path)
    return {
        "path": relative_path,
        "sha256": result.bundle_sha256,
        "size_bytes": result.bundle_size_bytes,
        "verified": True,
    }


def relative_bundle_path(
    plan: PreflightExecutionPlan,
    bundle_path: Path,
    state_path: Path | None,
) -> str:
    if state_path is not None:
        try:
            return bundle_path.relative_to(state_path.parent.parent).as_posix()
        except ValueError:
            pass
    roots = [Path(plan.context_root)]
    for root in roots:
        try:
            return bundle_path.relative_to(root).as_posix()
        except ValueError:
            continue
    return bundle_path.name


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _bundle_descriptor(
    state_path: Path,
    payload: dict[str, object],
) -> tuple[Path | None, str | None, int | None]:
    bundle = payload.get("bundle")
    if not isinstance(bundle, dict):
        return None, None, None
    relative_path = _string(bundle.get("path"))
    if relative_path is None:
        return None, None, None
    path = Path(relative_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError(
            f"Workspace lineage bundle path is unsafe: {state_path.as_posix()}"
        )
    return (
        state_path.parent.parent / path,
        _string(bundle.get("sha256")),
        _int(bundle.get("size_bytes")),
    )


def _upstream_sources(
    state_path: Path,
    payload: dict[str, object],
) -> tuple[WorktreeSourceRef, ...]:
    source = payload.get("source")
    if not isinstance(source, dict):
        return ()
    upstream = _source_ref_from_payload(state_path, source)
    return (upstream,) if upstream is not None else ()


def _source_ref_from_payload(
    state_path: Path,
    payload: dict[str, object],
) -> WorktreeSourceRef | None:
    source_kind = _string(payload.get("kind"))
    source_commit = _string(payload.get("commit"))
    source_tree = _string(payload.get("tree"))
    if source_kind not in {"project", "node", "candidate"}:
        return None
    if source_commit is None or source_tree is None:
        return None
    return WorktreeSourceRef(
        source_kind=source_kind,
        source_node_id=_string(payload.get("node_id")),
        source_commit=source_commit,
        source_tree=source_tree,
        candidate_sequence=_int(payload.get("candidate_sequence")),
        bundle_path=_source_bundle_path(state_path, payload),
        bundle_sha256=_string(payload.get("bundle_sha256")),
        bundle_size_bytes=_int(payload.get("bundle_size_bytes")),
        bundle_ref=_string(payload.get("bundle_ref")),
        upstream_sources=_nested_upstream_sources(state_path, payload),
    )


def _nested_upstream_sources(
    state_path: Path,
    payload: dict[str, object],
) -> tuple[WorktreeSourceRef, ...]:
    upstreams = payload.get("upstream_sources")
    if not isinstance(upstreams, list):
        return ()
    parsed = [
        source_ref
        for item in upstreams
        if isinstance(item, dict)
        for source_ref in [_source_ref_from_payload(state_path, item)]
        if source_ref is not None
    ]
    return tuple(parsed)


def _source_bundle_path(
    state_path: Path,
    payload: dict[str, object],
) -> Path | None:
    relative_path = _string(payload.get("bundle_path"))
    if relative_path is None:
        return None
    path = Path(relative_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError(
            f"Workspace lineage source bundle path is unsafe: {state_path.as_posix()}"
        )
    return state_path.parent.parent / path

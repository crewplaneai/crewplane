from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PersistedWorkspaceSourceDescriptor:
    source_kind: str
    source_node_id: str | None
    source_commit: str
    source_tree: str
    bundle_path: Path | None
    bundle_sha256: str | None
    bundle_size_bytes: int | None
    bundle_ref: str | None
    upstream_sources: tuple[PersistedWorkspaceSourceDescriptor, ...] = ()


def workspace_result_descriptor_from_payload(
    run_dir: Path,
    payload: Mapping[str, object],
) -> PersistedWorkspaceSourceDescriptor:
    """Decode a persisted workspace result and its recursive source chain."""

    source_payload = _mapping(payload.get("source"))
    result = _mapping(payload.get("result"))
    bundle = _mapping(payload.get("bundle"))
    refs = _mapping(payload.get("refs"))
    return PersistedWorkspaceSourceDescriptor(
        source_kind="node",
        source_node_id=_required_string(payload.get("node_id")),
        source_commit=_required_string(result.get("result_commit")),
        source_tree=_required_string(result.get("result_tree")),
        bundle_path=_contained_bundle_path(run_dir, bundle.get("path")),
        bundle_sha256=_required_string(bundle.get("sha256")),
        bundle_size_bytes=_required_int(bundle.get("size_bytes")),
        bundle_ref=_required_string(refs.get("result")),
        upstream_sources=(_source_descriptor_from_payload(run_dir, source_payload),),
    )


def _source_descriptor_from_payload(
    run_dir: Path,
    payload: Mapping[str, object],
) -> PersistedWorkspaceSourceDescriptor:
    upstream_payloads = payload.get("upstream_sources")
    if upstream_payloads is None:
        upstream_payloads = []
    if not isinstance(upstream_payloads, list) or not all(
        isinstance(item, dict) for item in upstream_payloads
    ):
        raise RuntimeError("Workspace source descriptor chain is invalid.")
    return PersistedWorkspaceSourceDescriptor(
        source_kind=_required_string(payload.get("kind")),
        source_node_id=_optional_string(payload.get("node_id")),
        source_commit=_required_string(payload.get("commit")),
        source_tree=_required_string(payload.get("tree")),
        bundle_path=(
            _contained_bundle_path(run_dir, payload.get("bundle_path"))
            if payload.get("bundle_path") is not None
            else None
        ),
        bundle_sha256=_optional_string(payload.get("bundle_sha256")),
        bundle_size_bytes=_optional_int(payload.get("bundle_size_bytes")),
        bundle_ref=_optional_string(payload.get("bundle_ref")),
        upstream_sources=tuple(
            _source_descriptor_from_payload(run_dir, item)
            for item in upstream_payloads
            if isinstance(item, dict)
        ),
    )


def _contained_bundle_path(run_dir: Path, value: object) -> Path:
    relative_path = _required_string(value)
    path = Path(relative_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError("Workspace source bundle path is unsafe.")
    candidate = run_dir / path
    try:
        resolved_run_dir = run_dir.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("Workspace source bundle path is missing.") from exc
    if not resolved_candidate.is_relative_to(resolved_run_dir):
        raise RuntimeError("Workspace source bundle path escapes the run directory.")
    return candidate


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _required_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError("Workspace source descriptor lacks a required string.")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _required_string(value)


def _required_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError("Workspace source descriptor lacks a required size.")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return _required_int(value)

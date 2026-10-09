"""Resolve text file tokens and register content-addressed preflight resources.

Resolution captures bytes synchronously without writing artifacts. Registration
mutates caller-owned compile state; callers retain ownership of occurrence maps.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from crewplane.architecture.contracts import JsonObject
from crewplane.core.platform import is_native_windows
from crewplane.core.state_paths import FILE_TOKEN_EXCLUDED_ROOTS, is_reserved_state_path

if TYPE_CHECKING:
    from crewplane.architecture.ports import TerminalHistoryReaderPort

from .compile_state import (
    CompileState,
    ResolvedStaticFileReference,
    extend_diagnostics,
)
from .diagnostics import (
    PreflightDiagnostic,
    PreflightDiagnosticCode,
    PreflightDiagnosticPhase,
)
from .models import StaticResource
from .signatures import signature_for_payload


@dataclass(frozen=True)
class StaticFileResult:
    """Captured resource and bytes, or diagnostics for a rejected file.

    Resolvers return both resource and payload on success, with no diagnostics.
    Rejections carry neither and contain one file-policy or encoding diagnostic.
    Construction does not enforce these states; registration skips results
    missing either resource or payload while still recording their diagnostics.
    """

    resource: StaticResource | None
    payload: bytes | None
    diagnostics: tuple[PreflightDiagnostic, ...] = ()


def prepare_static_file_reference(
    result: StaticFileResult,
    node_id: str,
    occurrence_id: str,
    raw_token: str,
    state: CompileState,
) -> ResolvedStaticFileReference | None:
    """Record diagnostics and register a copy signed for this token occurrence.

    Diagnostics are deduplicated in state before checking the result. Missing
    resource or payload returns None without resource registration. Success
    leaves the supplied resource unchanged and delegates content deduplication
    to append_static_resource; callers store the returned occurrence reference.
    """
    extend_diagnostics(state, result.diagnostics)
    if result.resource is None or result.payload is None:
        return None
    signature = static_file_token_signature(
        result.resource, node_id, occurrence_id, raw_token
    )
    resource = result.resource.model_copy(update={"token_signatures": [signature]})
    append_static_resource(state, resource, result.payload, signature)
    return ResolvedStaticFileReference(resource=resource, token_signature=signature)


def static_file_token_signature(
    resource: StaticResource,
    node_id: str,
    occurrence_id: str,
    raw_token: str,
) -> str:
    """Sign content identity, byte size, node, occurrence, and raw token.

    Resource paths and existing token signatures are excluded from the payload.
    """
    return signature_for_payload(
        {
            "content_ref": resource.content_ref,
            "node_id": node_id,
            "occurrence_id": occurrence_id,
            "raw_token": raw_token,
            "sha256": resource.sha256,
            "size_bytes": resource.size_bytes,
        }
    )


def static_file_resolved_payload(resource: StaticResource) -> JsonObject:
    """Return token-catalog content identity and provenance, with size as text."""
    return {
        "kind": "static_file_content",
        "content_ref": resource.content_ref,
        "content_sha256": resource.sha256,
        "content_size": str(resource.size_bytes),
        "resolved_path": resource.resolved_path,
        "source_root": resource.source_root,
    }


def static_file_metadata(resource: StaticResource) -> dict[str, str]:
    """Return a new token-catalog metadata mapping of content reference and hash."""
    return {"content_ref": resource.content_ref, "sha256": resource.sha256}


def append_static_resource(
    state: CompileState,
    resource: StaticResource,
    payload: bytes,
    token_signature: str,
) -> None:
    """Register content in state, deduplicating by content_ref.

    New entries store the supplied resource and bytes unchanged. A duplicate
    keeps the first resource's metadata and list position, replaces it with a
    copy containing sorted unique token signatures, and retains any stored
    payload. The supplied resource's token_signatures are never mutated.
    """
    for index, existing in enumerate(state.static_resources):
        if existing.content_ref != resource.content_ref:
            continue
        signatures = [*existing.token_signatures, token_signature]
        state.static_resources[index] = existing.model_copy(
            update={"token_signatures": sorted(set(signatures))}
        )
        state.static_payloads.setdefault(resource.content_ref, payload)
        return
    state.static_resources.append(resource)
    state.static_payloads[resource.content_ref] = payload


def resolve_static_file(
    raw_path: str,
    source_root: Path,
    project_root: Path,
    allowed_paths: tuple[Path, ...],
) -> StaticFileResult:
    """Capture UTF-8 text bytes under the project or an allowlisted path.

    Relative paths use source_root; project_root and allowed_paths are expected
    to be normalized by the caller. Reserved runtime paths are rejected before
    allowlist checks, both before existence testing and after strict resolution.
    Empty, forbidden, missing, non-file, non-UTF-8, or NUL-containing inputs
    return diagnostics. Successful payloads preserve the original bytes.
    Path expansion, resolution, and I/O exceptions propagate to the caller.
    """
    raw = raw_path.strip()
    if not raw:
        return _file_diagnostic(raw_path, "Template file path is empty.")

    normalized = _normalize_static_file_path(raw, source_root)
    if isinstance(normalized, StaticFileResult):
        return normalized
    diagnostic = _static_file_candidate_diagnostic(
        raw, normalized, project_root, allowed_paths
    )
    if diagnostic is not None:
        return diagnostic
    resolved = _resolve_existing_static_file_path(
        raw, normalized, project_root, allowed_paths
    )
    if isinstance(resolved, StaticFileResult):
        return resolved
    return _materialize_static_file(raw, source_root, resolved, resolved.read_bytes())


def _normalize_static_file_path(
    raw_path: str, source_root: Path
) -> Path | StaticFileResult:
    candidate = Path(raw_path).expanduser()
    if is_native_windows() and (
        bool(candidate.drive or candidate.root)
        and not candidate.is_absolute()
        or ":" in raw_path[len(candidate.drive) :]
    ):
        return _file_diagnostic(
            raw_path,
            "Use a relative path or a fully qualified native path without alternate data streams.",
        )
    if not candidate.is_absolute():
        candidate = source_root / candidate
    return candidate.resolve(strict=False)


def _static_file_candidate_diagnostic(
    raw_path: str,
    path: Path,
    project_root: Path,
    allowed_paths: tuple[Path, ...],
) -> StaticFileResult | None:
    diagnostic = _static_path_policy_diagnostic(
        raw_path,
        path,
        project_root,
        allowed_paths,
        f"Template access denied: {raw_path}",
    )
    if diagnostic is not None:
        return diagnostic
    if not path.exists():
        return _file_diagnostic(
            raw_path,
            f"File not found: {raw_path}",
            resolved_path=path,
        )
    return None


def _resolve_existing_static_file_path(
    raw_path: str,
    path: Path,
    project_root: Path,
    allowed_paths: tuple[Path, ...],
) -> Path | StaticFileResult:
    resolved = path.resolve(strict=True)
    diagnostic = _static_path_policy_diagnostic(
        raw_path,
        resolved,
        project_root,
        allowed_paths,
        f"Template access denied after symlink resolution: {raw_path}",
    )
    if diagnostic is not None:
        return diagnostic
    if not resolved.is_file():
        return _file_diagnostic(
            raw_path, f"Not a file: {raw_path}", resolved_path=resolved
        )
    return resolved


def _static_path_policy_diagnostic(
    raw_path: str,
    path: Path,
    project_root: Path,
    allowed_paths: tuple[Path, ...],
    access_denied_message: str,
) -> StaticFileResult | None:
    if _is_reserved_state_resource(path, project_root):
        return _file_diagnostic(
            raw_path,
            f"Template access denied for Crewplane runtime-owned path: {raw_path}",
            resolved_path=path,
        )
    if not _path_is_allowed(path, project_root, allowed_paths):
        return _file_diagnostic(
            raw_path,
            access_denied_message,
            resolved_path=path,
        )
    return None


def resolve_terminal_result_file(
    raw_path: str,
    source_root: Path,
    history_reader: TerminalHistoryReaderPort | None,
) -> StaticFileResult | None:
    """Materialize bytes supplied by the terminal-history reader without rereading.

    No reader or an unmatched path returns None so callers can try ordinary
    file resolution. A matched reader error becomes a file-policy diagnostic;
    matched bytes undergo the same text validation as ordinary files. The
    reader remains caller-owned, and its exceptions propagate.

    Raises:
        ValueError: A matched success lacks a path or payload.
    """
    if history_reader is None:
        return None
    raw = raw_path.strip()
    result = history_reader.read_terminal_result(raw, source_root)
    if not result.matched:
        return None
    if result.error is not None:
        return _file_diagnostic(
            raw,
            result.error,
            resolved_path=result.path,
        )
    if result.path is None or result.payload is None:
        raise ValueError("Terminal history reader returned an incomplete result.")
    return _materialize_static_file(raw, source_root, result.path, result.payload)


def _materialize_static_file(
    raw_path: str,
    source_root: Path,
    resolved_path: Path,
    payload: bytes,
) -> StaticFileResult:
    diagnostic = _static_file_encoding_diagnostic(raw_path, resolved_path, payload)
    if diagnostic is not None:
        return diagnostic
    digest = hashlib.sha256(payload).hexdigest()
    resource = StaticResource(
        resource_id=digest,
        kind="file",
        raw_path=raw_path,
        source_root=source_root.resolve(strict=False).as_posix(),
        resolved_path=resolved_path.as_posix(),
        content_ref=f"static-files/{digest}.txt",
        size_bytes=len(payload),
        sha256=digest,
    )
    return StaticFileResult(resource=resource, payload=payload)


def _static_file_encoding_diagnostic(
    raw_path: str, resolved_path: Path, payload: bytes
) -> StaticFileResult | None:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return _file_diagnostic(
            raw_path,
            "File token content must be UTF-8 text.",
            resolved_path,
            code=PreflightDiagnosticCode.FILE_ENCODING,
        )
    if "\x00" in text:
        return _file_diagnostic(
            raw_path,
            "File contains NUL bytes.",
            resolved_path,
            code=PreflightDiagnosticCode.FILE_ENCODING,
        )
    return None


def _path_is_allowed(
    path: Path, project_root: Path, allowed_paths: tuple[Path, ...]
) -> bool:
    if path.is_relative_to(project_root):
        return True
    return any(
        path == allowed or path.is_relative_to(allowed) for allowed in allowed_paths
    )


def _is_reserved_state_resource(path: Path, project_root: Path) -> bool:
    resolved_project_root = project_root.resolve(strict=False)
    try:
        relative_path = path.relative_to(resolved_project_root).as_posix()
    except ValueError:
        return False
    return is_reserved_state_path(relative_path, FILE_TOKEN_EXCLUDED_ROOTS)


def _file_diagnostic(
    raw_path: str,
    message: str,
    resolved_path: Path | None = None,
    code: PreflightDiagnosticCode = PreflightDiagnosticCode.FILE_POLICY,
) -> StaticFileResult:
    metadata: dict[str, str | int | bool | None] = {}
    if resolved_path is not None:
        metadata["resolved_path"] = resolved_path.as_posix()
    return StaticFileResult(
        resource=None,
        payload=None,
        diagnostics=(
            PreflightDiagnostic(
                code=code,
                phase=PreflightDiagnosticPhase.FILE_POLICY,
                message=message,
                path=raw_path,
                metadata=metadata,
            ),
        ),
    )

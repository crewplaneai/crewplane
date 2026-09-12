from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from crewplane.architecture.contracts import JsonObject
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
    return {
        "kind": "static_file_content",
        "content_ref": resource.content_ref,
        "content_sha256": resource.sha256,
        "content_size": str(resource.size_bytes),
        "resolved_path": resource.resolved_path,
        "source_root": resource.source_root,
    }


def static_file_metadata(resource: StaticResource) -> dict[str, str]:
    return {"content_ref": resource.content_ref, "sha256": resource.sha256}


def append_static_resource(
    state: CompileState,
    resource: StaticResource,
    payload: bytes,
    token_signature: str,
) -> None:
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
    raw = raw_path.strip()
    if not raw:
        return _file_diagnostic(raw_path, "Template file path is empty.")

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = source_root / candidate
    normalized = candidate.resolve(strict=False)
    if _is_reserved_state_resource(normalized, project_root):
        return _file_diagnostic(
            raw,
            f"Template access denied for Crewplane runtime-owned path: {raw}",
            resolved_path=normalized,
        )
    if not _path_is_allowed(normalized, project_root, allowed_paths):
        return _file_diagnostic(
            raw,
            f"Template access denied: {raw}",
            resolved_path=normalized,
        )
    if not normalized.exists():
        return _file_diagnostic(
            raw,
            f"File not found: {raw}",
            resolved_path=normalized,
        )
    resolved = normalized.resolve(strict=True)
    if _is_reserved_state_resource(resolved, project_root):
        return _file_diagnostic(
            raw,
            f"Template access denied for Crewplane runtime-owned path: {raw}",
            resolved_path=resolved,
        )
    if not _path_is_allowed(resolved, project_root, allowed_paths):
        return _file_diagnostic(
            raw,
            f"Template access denied after symlink resolution: {raw}",
            resolved_path=resolved,
        )
    if not resolved.is_file():
        return _file_diagnostic(raw, f"Not a file: {raw}", resolved_path=resolved)
    return _materialize_static_file(raw, source_root, resolved, resolved.read_bytes())


def resolve_terminal_result_file(
    raw_path: str,
    source_root: Path,
    history_reader: TerminalHistoryReaderPort | None,
) -> StaticFileResult | None:
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
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return _encoding_diagnostic(raw_path, resolved_path)
    if "\x00" in text:
        return _encoding_diagnostic(
            raw_path,
            resolved_path,
            "File contains NUL bytes.",
        )
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
) -> StaticFileResult:
    metadata: dict[str, str | int | bool | None] = {}
    if resolved_path is not None:
        metadata["resolved_path"] = resolved_path.as_posix()
    return StaticFileResult(
        resource=None,
        payload=None,
        diagnostics=(
            PreflightDiagnostic(
                code=PreflightDiagnosticCode.FILE_POLICY,
                phase=PreflightDiagnosticPhase.FILE_POLICY,
                message=message,
                path=raw_path,
                metadata=metadata,
            ),
        ),
    )


def _encoding_diagnostic(
    raw_path: str,
    resolved_path: Path,
    message: str = "File token content must be UTF-8 text.",
) -> StaticFileResult:
    return StaticFileResult(
        resource=None,
        payload=None,
        diagnostics=(
            PreflightDiagnostic(
                code=PreflightDiagnosticCode.FILE_ENCODING,
                phase=PreflightDiagnosticPhase.FILE_POLICY,
                message=message,
                path=raw_path,
                metadata={"resolved_path": resolved_path.as_posix()},
            ),
        ),
    )

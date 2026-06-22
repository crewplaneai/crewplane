from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .compile_state import CompileState
from .diagnostics import (
    PreflightDiagnostic,
    PreflightDiagnosticCode,
    PreflightDiagnosticPhase,
)
from .models import StaticResource


@dataclass(frozen=True)
class StaticFileResult:
    resource: StaticResource | None
    payload: bytes | None
    diagnostics: tuple[PreflightDiagnostic, ...] = ()


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
    if not _path_is_allowed(resolved, project_root, allowed_paths):
        return _file_diagnostic(
            raw,
            f"Template access denied after symlink resolution: {raw}",
            resolved_path=resolved,
        )
    if not resolved.is_file():
        return _file_diagnostic(raw, f"Not a file: {raw}", resolved_path=resolved)
    payload = resolved.read_bytes()
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return _encoding_diagnostic(raw, resolved)
    if "\x00" in text:
        return _encoding_diagnostic(raw, resolved, "File contains NUL bytes.")
    digest = hashlib.sha256(payload).hexdigest()
    resource = StaticResource(
        resource_id=digest,
        kind="file",
        raw_path=raw,
        source_root=source_root.resolve(strict=False).as_posix(),
        resolved_path=resolved.as_posix(),
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


def _file_diagnostic(
    raw_path: str,
    message: str,
    resolved_path: Path | None = None,
) -> StaticFileResult:
    metadata = {}
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

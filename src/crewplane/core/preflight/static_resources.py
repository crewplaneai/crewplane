from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from crewplane.artifacts.safe_files import contained_regular_file

from .compile_state import CompileState
from .diagnostics import (
    PreflightDiagnostic,
    PreflightDiagnosticCode,
    PreflightDiagnosticPhase,
)
from .models import StaticResource

RESERVED_STATE_RESOURCE_ROOTS = (
    ".crewplane/execution-stages",
    ".crewplane/execution-results",
    ".crewplane/preflight",
    ".crewplane/locks",
)


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
    state_dir: Path,
) -> StaticFileResult | None:
    raw = raw_path.strip()
    relative_path = _terminal_result_relative_path(raw, source_root, state_dir)
    if relative_path is None:
        return None
    if len(relative_path.parts) < 2:
        return _file_diagnostic(raw, "Execution result path is incomplete.")

    run_key_name = relative_path.parts[0]
    manifest_error = _validate_terminal_run(raw, state_dir, run_key_name)
    if manifest_error is not None:
        return manifest_error

    file_result = _read_terminal_result(raw, state_dir, relative_path)
    if isinstance(file_result, StaticFileResult):
        return file_result
    result_path, payload = file_result
    return _materialize_static_file(raw, source_root, result_path, payload)


def _terminal_result_relative_path(
    raw_path: str,
    source_root: Path,
    state_dir: Path,
) -> Path | None:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = source_root / candidate
    candidate = Path(os.path.abspath(candidate))
    results_root = Path(os.path.abspath(state_dir / "execution-results"))
    try:
        return candidate.relative_to(results_root)
    except ValueError:
        return None


def _validate_terminal_run(
    raw_path: str,
    state_dir: Path,
    run_key_name: str,
) -> StaticFileResult | None:
    from crewplane.core.execution_state import RunManifest

    manifest_path = contained_regular_file(
        state_dir / "execution-stages",
        f"{run_key_name}/manifests/run.json",
    )
    if manifest_path is None:
        return _file_diagnostic(
            raw_path,
            "Execution result run manifest is missing or unsafe.",
        )
    try:
        manifest = RunManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValidationError):
        return _file_diagnostic(
            raw_path,
            "Execution result run manifest is invalid.",
            resolved_path=manifest_path,
        )
    if manifest.run_key_name != run_key_name:
        return _file_diagnostic(
            raw_path,
            "Execution result run manifest does not match its run directory.",
            resolved_path=manifest_path,
        )
    if manifest.status == "running":
        return _file_diagnostic(
            raw_path,
            "Execution result run is still running.",
            resolved_path=manifest_path,
        )
    return None


def _read_terminal_result(
    raw_path: str,
    state_dir: Path,
    relative_path: Path,
) -> tuple[Path, bytes] | StaticFileResult:
    result_path = contained_regular_file(
        state_dir / "execution-results",
        relative_path.as_posix(),
    )
    if result_path is None:
        return _file_diagnostic(
            raw_path,
            "Execution result is missing or is not a safe regular file.",
        )
    try:
        return result_path, result_path.read_bytes()
    except OSError:
        return _file_diagnostic(
            raw_path,
            "Execution result could not be read.",
            resolved_path=result_path,
        )


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
    return any(
        relative_path == root or relative_path.startswith(f"{root}/")
        for root in RESERVED_STATE_RESOURCE_ROOTS
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

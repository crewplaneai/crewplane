from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from crewplane.architecture.ports import TerminalHistoryRead
from crewplane.architecture.safe_files import contained_regular_file
from crewplane.core.execution_state import RUN_STATUS_RUNNING, RunManifest

_EXECUTION_RESULTS_DIR = "execution-results"
_EXECUTION_STAGES_DIR = "execution-stages"


@dataclass(frozen=True)
class _ResultRelativePath:
    run_key_name: str
    relative_path: Path
    is_complete: bool


@dataclass(frozen=True)
class FilesystemTerminalHistoryReader:
    """Safely read terminal result artifacts from the filesystem store."""

    state_dir: Path

    def read_terminal_result(
        self,
        raw_path: str,
        source_root: Path,
    ) -> TerminalHistoryRead:
        result_location = self._result_relative_path(raw_path, source_root)
        if result_location is None:
            return TerminalHistoryRead(matched=False)

        path_error = self._validate_result_path_structure(result_location)
        if path_error is not None:
            return path_error

        manifest_error = self._terminal_manifest_error(result_location.run_key_name)
        if manifest_error is not None:
            return manifest_error

        result_path = self._resolve_result_file(result_location.relative_path)
        if result_path is None:
            return self._matched_error(
                "Execution result is missing or is not a safe regular file."
            )

        return self._read_result_bytes(result_path)

    def _validate_result_path_structure(
        self,
        result_location: _ResultRelativePath,
    ) -> TerminalHistoryRead | None:
        if not result_location.is_complete:
            return self._matched_error("Execution result path is incomplete.")
        return None

    def _resolve_result_file(self, relative_path: Path) -> Path | None:
        return contained_regular_file(
            self.state_dir / _EXECUTION_RESULTS_DIR,
            relative_path.as_posix(),
        )

    def _terminal_manifest_error(
        self,
        run_key_name: str,
    ) -> TerminalHistoryRead | None:
        manifest_path = self._terminal_manifest_path(run_key_name)
        if manifest_path is None:
            return self._matched_error(
                "Execution result run manifest is missing or unsafe."
            )
        manifest = self._load_terminal_manifest(manifest_path)
        if isinstance(manifest, TerminalHistoryRead):
            return manifest
        return self._validate_terminal_manifest(manifest, manifest_path, run_key_name)

    def _terminal_manifest_path(self, run_key_name: str) -> Path | None:
        return contained_regular_file(
            self.state_dir / _EXECUTION_STAGES_DIR,
            f"{run_key_name}/manifests/run.json",
        )

    def _load_terminal_manifest(
        self,
        manifest_path: Path,
    ) -> RunManifest | TerminalHistoryRead:
        try:
            manifest = RunManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, ValidationError):
            return self._matched_error(
                "Execution result run manifest is invalid.",
                path=manifest_path,
            )
        return manifest

    def _validate_terminal_manifest(
        self,
        manifest: RunManifest,
        manifest_path: Path,
        run_key_name: str,
    ) -> TerminalHistoryRead | None:
        if manifest.run_key_name != run_key_name:
            return self._matched_error(
                "Execution result run manifest does not match its run directory.",
                path=manifest_path,
            )
        if manifest.status == RUN_STATUS_RUNNING:
            return self._matched_error(
                "Execution result run is still running.",
                path=manifest_path,
            )
        return None

    def _result_relative_path(
        self,
        raw_path: str,
        source_root: Path,
    ) -> _ResultRelativePath | None:
        try:
            candidate = Path(raw_path).expanduser()
        except RuntimeError:
            return None
        if not candidate.is_absolute():
            candidate = source_root / candidate
        normalized_candidate = Path(os.path.abspath(candidate))
        results_root = Path(
            os.path.abspath(self.state_dir / _EXECUTION_RESULTS_DIR),
        )
        try:
            relative = normalized_candidate.relative_to(results_root)
        except ValueError:
            return None
        return _ResultRelativePath(
            run_key_name=relative.parts[0] if relative.parts else "",
            relative_path=relative,
            is_complete=len(relative.parts) >= 2,
        )

    def _read_result_bytes(self, result_path: Path) -> TerminalHistoryRead:
        try:
            payload = result_path.read_bytes()
        except OSError:
            return self._matched_error(
                "Execution result could not be read.",
                path=result_path,
            )
        return TerminalHistoryRead(
            matched=True,
            path=result_path,
            payload=payload,
        )

    @staticmethod
    def _matched_error(
        message: str,
        path: Path | None = None,
    ) -> TerminalHistoryRead:
        return TerminalHistoryRead(
            matched=True,
            path=path,
            error=message,
        )

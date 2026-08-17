from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from crewplane.architecture.ports import TerminalHistoryRead
from crewplane.architecture.safe_files import contained_regular_file
from crewplane.core.execution_state import RunManifest


@dataclass(frozen=True)
class FilesystemTerminalHistoryReader:
    """Safely read terminal result artifacts from the filesystem store."""

    state_dir: Path

    def read_terminal_result(
        self,
        raw_path: str,
        source_root: Path,
    ) -> TerminalHistoryRead:
        relative_path = self._result_relative_path(raw_path, source_root)
        if relative_path is None:
            return TerminalHistoryRead(matched=False)
        if len(relative_path.parts) < 2:
            return TerminalHistoryRead(
                matched=True,
                error="Execution result path is incomplete.",
            )
        run_key_name = relative_path.parts[0]
        manifest_error = self._terminal_manifest_error(run_key_name)
        if manifest_error is not None:
            return manifest_error
        result_path = contained_regular_file(
            self.state_dir / "execution-results",
            relative_path.as_posix(),
        )
        if result_path is None:
            return TerminalHistoryRead(
                matched=True,
                error="Execution result is missing or is not a safe regular file.",
            )
        try:
            payload = result_path.read_bytes()
        except OSError:
            return TerminalHistoryRead(
                matched=True,
                path=result_path,
                error="Execution result could not be read.",
            )
        return TerminalHistoryRead(
            matched=True,
            path=result_path,
            payload=payload,
        )

    def _result_relative_path(
        self,
        raw_path: str,
        source_root: Path,
    ) -> Path | None:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = source_root / candidate
        candidate = Path(os.path.abspath(candidate))
        results_root = Path(os.path.abspath(self.state_dir / "execution-results"))
        try:
            return candidate.relative_to(results_root)
        except ValueError:
            return None

    def _terminal_manifest_error(
        self,
        run_key_name: str,
    ) -> TerminalHistoryRead | None:
        manifest_path = contained_regular_file(
            self.state_dir / "execution-stages",
            f"{run_key_name}/manifests/run.json",
        )
        if manifest_path is None:
            return TerminalHistoryRead(
                matched=True,
                error="Execution result run manifest is missing or unsafe.",
            )
        try:
            manifest = RunManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, ValidationError):
            return TerminalHistoryRead(
                matched=True,
                path=manifest_path,
                error="Execution result run manifest is invalid.",
            )
        if manifest.run_key_name != run_key_name:
            return TerminalHistoryRead(
                matched=True,
                path=manifest_path,
                error="Execution result run manifest does not match its run directory.",
            )
        if manifest.status == "running":
            return TerminalHistoryRead(
                matched=True,
                path=manifest_path,
                error="Execution result run is still running.",
            )
        return None

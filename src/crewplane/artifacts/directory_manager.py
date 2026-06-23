from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from pathlib import Path

from crewplane.core.workflow.keywords import RESERVED_RUN_ROOT_NAMES

from .naming import (
    build_findings_filename,
    build_log_filename,
    build_result_filename,
    build_run_key_name,
    build_stage_directory_name,
    safe_artifact_name,
    safe_stage_name,
)


def _is_dot_segment(name: str) -> bool:
    return name.strip() in {".", ".."}


class DirectoryManager:
    """Manage output directories for a single task run."""

    def __init__(
        self,
        task_name: str,
        base_dir: Path,
        log_cli_output: bool,
    ) -> None:
        self.base_dir = base_dir.resolve()
        self._workflow_name = task_name
        self.task_name = safe_artifact_name(task_name)
        self.log_cli_output = log_cli_output
        (
            self.run_id,
            self.run_key_name,
            self.stages_dir,
            self.results_dir,
        ) = self._create_run_dirs()

        self.logs_dir = self.stages_dir / "logs"
        self.manifests_dir = self.stages_dir / "manifests"

        self._current_stage_dirs: dict[str, Path] = {}

    def create_stage_dir(self, stage_name: str) -> Path:
        self._validate_stage_name(stage_name)
        stage_dir = self.stages_dir / build_stage_directory_name(stage_name)
        stage_dir.mkdir(parents=True, exist_ok=True)
        self._current_stage_dirs[stage_name] = stage_dir
        return stage_dir

    def get_stage_dir(self, stage_name: str) -> Path | None:
        return self._current_stage_dirs.get(stage_name)

    def get_stage_result_file(self, stage_name: str) -> Path:
        self._validate_stage_name(stage_name)
        return self.results_dir / build_result_filename(stage_name)

    def get_stage_findings_file(self, stage_name: str) -> Path:
        self._validate_stage_name(stage_name)
        return self.results_dir / build_findings_filename(stage_name)

    def ensure_run_logs_dir(self) -> Path:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        return self.logs_dir

    def get_run_event_log_path(self) -> Path:
        return self.ensure_run_logs_dir() / "events.ndjson"

    def get_run_summary_path(self) -> Path:
        return self.ensure_run_logs_dir() / "summary.md"

    def get_log_file(
        self,
        stage_name: str,
        provider: str,
        task_id: str,
        audit_round_num: int | None = None,
        round_num: int | None = None,
    ) -> Path | None:
        if not self.log_cli_output:
            return None

        stage_dir = self.create_stage_dir(stage_name)
        provider_dir = stage_dir / "logs" / safe_artifact_name(provider)
        provider_dir.mkdir(parents=True, exist_ok=True)

        filename = build_log_filename(task_id, audit_round_num, round_num)
        return provider_dir / filename

    def ensure_manifests_dir(self) -> Path:
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        return self.manifests_dir

    @staticmethod
    def _validate_stage_name(stage_name: str) -> None:
        if _is_dot_segment(stage_name):
            raise ValueError("Stage name cannot be '.' or '..'.")
        normalized = safe_stage_name(stage_name)
        if normalized in RESERVED_RUN_ROOT_NAMES:
            reserved = ", ".join(sorted(RESERVED_RUN_ROOT_NAMES))
            raise ValueError(
                f"Stage name '{stage_name}' is reserved. Names cannot be: {reserved}."
            )

    def _create_run_dirs(self) -> tuple[str, str, Path, Path]:
        now = datetime.now()
        run_id = now.strftime("%Y%m%d-%H%M%S")
        run_paths = self._run_paths(run_id)
        try:
            self._create_stage_run_dir(*run_paths)
            return run_id, build_run_key_name(self._workflow_name, run_id), *run_paths
        except FileExistsError:
            retry_run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            retry_paths = self._run_paths(retry_run_id)
            try:
                self._create_stage_run_dir(*retry_paths)
                return (
                    retry_run_id,
                    build_run_key_name(self._workflow_name, retry_run_id),
                    *retry_paths,
                )
            except FileExistsError as exc:
                raise RuntimeError(
                    "Unable to allocate unique run directories after retrying with "
                    "microsecond precision."
                ) from exc

    def _run_paths(self, run_id: str) -> tuple[Path, Path]:
        run_key = build_run_key_name(self._workflow_name, run_id)
        stages_dir = self.base_dir / "execution-stages" / run_key
        results_dir = self.base_dir / "execution-results" / run_key
        return stages_dir, results_dir

    @staticmethod
    def _create_stage_run_dir(stages_dir: Path, results_dir: Path) -> None:
        stages_created = False
        try:
            stages_dir.mkdir(parents=True, exist_ok=False)
            stages_created = True
            if results_dir.exists():
                raise FileExistsError(results_dir)
        except FileExistsError:
            if stages_created:
                with suppress(OSError):
                    stages_dir.rmdir()
            raise

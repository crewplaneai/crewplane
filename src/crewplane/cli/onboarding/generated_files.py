"""Preparation, persistence, and recovery of generated onboarding files."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from crewplane.core.config import AgentConfig
from crewplane.core.state_paths import STATE_DIR_NAME

from ..project_init import initialize_project_templates
from . import messages
from .constants import CONFIG_RELATIVE_PATH, WORKFLOW_RELATIVE_PATH
from .rendering import (
    OnboardingRenderingError,
    render_provider_ready_config,
    render_provider_ready_workflow,
)

if TYPE_CHECKING:
    from .runner import OnboardingOptions, OnboardingProjectState


@dataclass(frozen=True)
class WriteResult:
    """Relative paths saved or rejected, each in attempted write order."""

    updated: tuple[Path, ...]
    not_updated: tuple[Path, ...]

    @property
    def complete(self) -> bool:
        return not self.not_updated


class GeneratedOnboardingFiles:
    """Own sequential generated-file writes and this attempt's recovery state.

    Comparisons detect observed edits without locking concurrent writers. Writes
    are independent and non-transactional; only complete saves become recoverable.
    """

    def __init__(
        self, options: OnboardingOptions, confirm: Callable[[str, bool], bool]
    ) -> None:
        self.options = options
        self.console = options.console
        self.confirm = confirm
        self.written_files: tuple[str, str] | None = None

    def path(self, relative_path: Path) -> Path:
        return self.options.project_root / relative_path

    def write_result_is_complete(self, write_result: WriteResult | None) -> bool:
        if write_result is None:
            return False
        if write_result.complete:
            return True
        self.print_incomplete_write_result(write_result)
        return False

    def print_incomplete_write_result(self, write_result: WriteResult) -> None:
        if len(write_result.updated) == 1:
            messages.print_partial_update(
                self.console, write_result.updated, write_result.not_updated
            )
            return
        messages.print_no_files_updated(self.console)

    def ensure_generated_files(self) -> bool:
        state_dir = self.options.project_root / STATE_DIR_NAME
        if not state_dir.exists():
            return self.handle_missing_state_dir()
        if self.generated_files_exist():
            return True
        return self.handle_missing_generated_files()

    def handle_missing_state_dir(self) -> bool:
        messages.print_missing_state_dir(self.console, self.options.project_root)
        return self._recover_missing_generated_files()

    def handle_missing_generated_files(self) -> bool:
        messages.print_missing_generated_files(self.console)
        return self._recover_missing_generated_files()

    def _recover_missing_generated_files(self) -> bool:
        if not self.confirm(messages.INIT_RECOVERY_PROMPT, False):
            messages.print_no_files_changed(self.console)
            return False
        initialize_project_templates(self.console, self.options.project_root)
        return self.generated_files_exist()

    def generated_files_exist(self) -> bool:
        return (
            self.path(CONFIG_RELATIVE_PATH).is_file()
            and self.path(WORKFLOW_RELATIVE_PATH).is_file()
        )

    def workflow_matches_default(self, default_workflow: str) -> bool:
        try:
            return (
                self.path(WORKFLOW_RELATIVE_PATH).read_text(encoding="utf-8")
                == default_workflow
            )
        except OSError:
            return False

    def generated_files_are_safe_to_update(
        self, default_config: str, default_workflow: str
    ) -> bool:
        try:
            if self.files_match_defaults(default_config, default_workflow):
                return True
        except (OSError, UnicodeError) as exc:
            messages.print_user_owned_stop(
                self.console,
                f"Onboarding cannot confirm generated files are unchanged: {exc}",
            )
            return False
        messages.print_edited_files_stop(self.console)
        return False

    def files_match_defaults(self, default_config: str, default_workflow: str) -> bool:
        return (
            self.path(CONFIG_RELATIVE_PATH).read_text(encoding="utf-8")
            == default_config
            and self.path(WORKFLOW_RELATIVE_PATH).read_text(encoding="utf-8")
            == default_workflow
        )

    def prepare_files(
        self,
        providers: tuple[str, ...],
        project_state: OnboardingProjectState,
        local_agent: AgentConfig | None,
    ) -> WriteResult | None:
        if not self.recheck_generated_files(
            providers, project_state.default_config, project_state.default_workflow
        ):
            return None
        try:
            config_text = render_provider_ready_config(
                project_state.default_config, providers, local_agent
            )
            workflow_text = render_provider_ready_workflow(
                project_state.default_workflow, providers
            )
        except OnboardingRenderingError as exc:
            messages.print_manual_fallback(self.console, providers, str(exc))
            return None

        return self.write_prepared_files(
            config_text, workflow_text, ", ".join(providers)
        )

    def recheck_generated_files(
        self, providers: tuple[str, ...], default_config: str, default_workflow: str
    ) -> bool:
        try:
            if self.files_match_defaults(default_config, default_workflow):
                return True
        except (OSError, UnicodeError) as exc:
            messages.print_manual_fallback(
                self.console,
                providers,
                f"Generated files could not be re-read before writing: {exc}",
            )
            return False
        messages.print_manual_fallback(
            self.console, providers, "Generated files changed before writing."
        )
        return False

    def write_prepared_files(
        self, config_text: str, workflow_text: str, provider: str
    ) -> WriteResult:
        provider = messages.provider_display_name(provider)
        updates = (
            (CONFIG_RELATIVE_PATH, config_text, f"for {provider}"),
            (WORKFLOW_RELATIVE_PATH, workflow_text, f"to use {provider}"),
        )
        updated: list[Path] = []
        not_updated: list[Path] = []
        for relative_path, content, message in updates:
            if self._write_file(relative_path, content, message):
                updated.append(relative_path)
            else:
                not_updated.append(relative_path)
        result = WriteResult(tuple(updated), tuple(not_updated))
        if result.complete:
            self.written_files = (config_text, workflow_text)
        return result

    def _write_file(self, relative_path: Path, content: str, message: str) -> bool:
        try:
            self.options.write_text(self.path(relative_path), content)
        except OSError as exc:
            messages.print_write_failure(self.console, relative_path, str(exc))
            return False
        messages.print_write_success(self.console, relative_path, message)
        return True

    def replace_saved_setup(
        self, providers: tuple[str, ...], project_state: OnboardingProjectState
    ) -> bool:
        messages.print_recovery_confirmation(self.console, ", ".join(providers))
        if not self.confirm(messages.APPLY_CHANGES_PROMPT, True):
            return False
        assert self.written_files is not None
        try:
            replacement = self._replacement_text(
                providers, project_state, self.written_files
            )
        except (OSError, UnicodeError, OnboardingRenderingError) as exc:
            messages.print_user_owned_stop(self.console, str(exc))
            return False
        if replacement is None:
            return False
        result = self.write_prepared_files(*replacement, ", ".join(providers) or "mock")
        return self.write_result_is_complete(result)

    def _replacement_text(
        self,
        providers: tuple[str, ...],
        project_state: OnboardingProjectState,
        saved_files: tuple[str, str],
    ) -> tuple[str, str] | None:
        if not self.files_match_defaults(*saved_files):
            messages.print_user_owned_stop(
                self.console, "Files changed after onboarding saved them."
            )
            return None
        config_text = project_state.default_config
        workflow_text = project_state.default_workflow
        if providers:
            config_text = render_provider_ready_config(config_text, providers)
            workflow_text = render_provider_ready_workflow(workflow_text, providers)
        return config_text, workflow_text

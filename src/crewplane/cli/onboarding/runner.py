from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console

from crewplane.core.config import Config, load_config
from crewplane.core.preflight import load_workflow_source_for_preflight
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.core.state_paths import STATE_DIR_NAME

from .. import workflow_runner
from ..project_init import initialize_project_templates
from ..run.preflight import uses_cli_invoker, uses_mock_invoker
from . import messages
from .constants import (
    CONFIG_RELATIVE_PATH,
    ONBOARDING_COMMAND_HELP,
    PROVIDER_SETUP_URL,
    WORKFLOW_RELATIVE_PATH,
)
from .history import find_successful_mock_run_evidence
from .rendering import (
    KNOWN_PROVIDER_NAMES,
    OnboardingRenderingError,
    render_provider_ready_config,
    render_provider_ready_workflow,
    rendered_default_config,
    rendered_default_workflow,
)

TextWriter = Callable[[Path, str], None]
WhichFunction = Callable[[str], str | None]
ReadInput = Callable[[], str]


@dataclass(frozen=True)
class OnboardingOptions:
    project_root: Path
    console: Console
    input_is_terminal: bool
    output_is_terminal: bool
    read_input: ReadInput
    which_fn: WhichFunction
    write_text: TextWriter


@dataclass(frozen=True)
class ProviderDetection:
    provider: str
    executable_path: str | None

    @property
    def found(self) -> bool:
        return self.executable_path is not None


@dataclass(frozen=True)
class WriteResult:
    updated: tuple[Path, ...]
    not_updated: tuple[Path, ...]

    @property
    def complete(self) -> bool:
        return not self.not_updated


@dataclass(frozen=True)
class OnboardingProjectState:
    config: Config
    workflow_path: Path
    default_config: str
    default_workflow: str


def run_onboarding_command() -> None:
    console = Console()
    run_onboarding(default_onboarding_options(console))


def default_onboarding_options(console: Console) -> OnboardingOptions:
    return OnboardingOptions(
        project_root=Path.cwd().resolve(strict=False),
        console=console,
        input_is_terminal=sys.stdin.isatty(),
        output_is_terminal=sys.stdout.isatty(),
        read_input=read_stdin_line,
        which_fn=shutil.which,
        write_text=write_text_file,
    )


def read_stdin_line() -> str:
    return sys.stdin.readline().rstrip("\n")


def write_text_file(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def run_onboarding(options: OnboardingOptions) -> None:
    OnboardingRunner(options).run()


class OnboardingRunner:
    def __init__(self, options: OnboardingOptions) -> None:
        self.options = options
        self.console = options.console

    def run(self) -> None:
        messages.print_title(self.console)
        project_state = self.load_project_state()
        if project_state is None:
            return

        provider = self.choose_onboarding_provider(project_state)
        if provider is None:
            return

        if not self.apply_provider_handoff(provider, project_state):
            return

        self.validate_provider_ready_setup(provider)
        messages.print_final_success(self.console, provider)

    def load_project_state(self) -> OnboardingProjectState | None:
        if not self.is_interactive():
            messages.print_non_tty_stop(self.console)
            return None
        if not self.ensure_generated_files():
            return None

        config = self.load_config_for_onboarding()
        if config is None:
            return None

        default_config = rendered_default_config()
        default_workflow = rendered_default_workflow()
        if not self.config_can_be_onboarded(config, default_workflow):
            return None
        if not self.generated_files_are_safe_to_update(
            default_config, default_workflow
        ):
            return None

        return OnboardingProjectState(
            config=config,
            workflow_path=self.path(WORKFLOW_RELATIVE_PATH),
            default_config=default_config,
            default_workflow=default_workflow,
        )

    def load_config_for_onboarding(self) -> Config | None:
        try:
            return load_config(self.path(CONFIG_RELATIVE_PATH))
        except Exception as exc:
            messages.print_user_owned_stop(
                self.console, f"Config could not be loaded: {exc}"
            )
            return None

    def config_can_be_onboarded(self, config: Config, default_workflow: str) -> bool:
        if uses_cli_invoker(config):
            self.print_existing_cli_config_stop(default_workflow)
            return False
        if uses_mock_invoker(config):
            return True
        messages.print_user_owned_stop(
            self.console, "Active invoker is not the generated mock setup."
        )
        return False

    def print_existing_cli_config_stop(self, default_workflow: str) -> None:
        if self.workflow_matches_default(default_workflow):
            messages.print_partial_update(
                self.console,
                (CONFIG_RELATIVE_PATH,),
                (WORKFLOW_RELATIVE_PATH,),
            )
            return
        messages.print_already_onboarded(self.console)

    def choose_onboarding_provider(
        self, project_state: OnboardingProjectState
    ) -> str | None:
        messages.print_onboarding_intro(self.console)
        source = load_workflow_source_for_preflight(
            project_state.workflow_path,
            project_root=self.options.project_root,
        )
        if not self.confirm_mock_run_evidence(project_state.config, source):
            return None

        found = self.found_provider_detections()
        if not found:
            return None

        provider = self.choose_provider(found)
        if provider is None:
            messages.print_provider_skip(self.console)
            return None
        messages.print_provider_selected(self.console, provider)
        return provider

    def found_provider_detections(self) -> tuple[ProviderDetection, ...]:
        detections = self.detect_providers()
        found = tuple(detection for detection in detections if detection.found)
        if not found:
            messages.print_no_providers_stop(self.console)
        return found

    def apply_provider_handoff(
        self, provider: str, project_state: OnboardingProjectState
    ) -> bool:
        if not self.confirm_file_preparation(provider):
            messages.print_declined_changes(self.console)
            return False
        write_result = self.prepare_files(
            provider,
            project_state.default_config,
            project_state.default_workflow,
        )
        return self.write_result_is_complete(write_result)

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

    def is_interactive(self) -> bool:
        return self.options.input_is_terminal and self.options.output_is_terminal

    def path(self, relative_path: Path) -> Path:
        return self.options.project_root / relative_path

    def ensure_generated_files(self) -> bool:
        state_dir = self.options.project_root / STATE_DIR_NAME
        if not state_dir.exists():
            return self.handle_missing_state_dir()
        if self.generated_files_exist():
            return True
        return self.handle_missing_generated_files()

    def handle_missing_state_dir(self) -> bool:
        messages.print_missing_state_dir(self.console, self.options.project_root)
        if not self.confirm(messages.INIT_RECOVERY_PROMPT, False):
            messages.print_no_files_changed(self.console)
            return False
        initialize_project_templates(self.console, self.options.project_root)
        return self.generated_files_exist()

    def handle_missing_generated_files(self) -> bool:
        messages.print_missing_generated_files(self.console)
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

    def confirm_mock_run_evidence(
        self, config: Config, source: PreflightWorkflowSource
    ) -> bool:
        messages.print_quickstart_state(self.console)
        evidence = find_successful_mock_run_evidence(
            self.options.project_root,
            self.options.project_root / STATE_DIR_NAME,
            config,
            source,
            self.console,
        )
        if evidence.found:
            messages.print_mock_run_evidence_found(self.console)
            return True
        if evidence.warning is not None:
            messages.print_mock_run_evidence_warning(self.console, evidence.warning)
        messages.print_missing_mock_run_evidence(self.console)
        return (
            self.prompt_choice(messages.MISSING_MOCK_EVIDENCE_PROMPT, ("0", "1"), "0")
            == "1"
        )

    def detect_providers(self) -> tuple[ProviderDetection, ...]:
        detections = tuple(
            ProviderDetection(provider, self.options.which_fn(provider))
            for provider in KNOWN_PROVIDER_NAMES
        )
        messages.print_provider_detection(
            self.console,
            tuple((detection.provider, detection.found) for detection in detections),
        )
        return detections

    def choose_provider(self, detections: tuple[ProviderDetection, ...]) -> str | None:
        providers = tuple(detection.provider for detection in detections)
        messages.print_provider_choices(self.console, providers)
        choices = tuple(str(index) for index in range(0, len(detections) + 1))
        selection = self.prompt_choice(messages.PROVIDER_CHOICE_PROMPT, choices, "0")
        if selection == "0":
            return None
        return providers[int(selection) - 1]

    def confirm_file_preparation(self, provider: str) -> bool:
        messages.print_file_preparation_confirmation(self.console, provider)
        return self.confirm(messages.APPLY_CHANGES_PROMPT, True)

    def prepare_files(
        self, provider: str, default_config: str, default_workflow: str
    ) -> WriteResult | None:
        try:
            files_still_match = self.files_match_defaults(
                default_config, default_workflow
            )
        except (OSError, UnicodeError) as exc:
            messages.print_manual_fallback(
                self.console,
                provider,
                f"Generated files could not be re-read before writing: {exc}",
            )
            return None
        if not files_still_match:
            messages.print_manual_fallback(
                self.console, provider, "Generated files changed before writing."
            )
            return None
        try:
            config_text = render_provider_ready_config(default_config, provider)
            workflow_text = render_provider_ready_workflow(default_workflow, provider)
        except OnboardingRenderingError as exc:
            messages.print_manual_fallback(self.console, provider, str(exc))
            return None

        return self.write_prepared_files(config_text, workflow_text, provider)

    def write_prepared_files(
        self, config_text: str, workflow_text: str, provider: str
    ) -> WriteResult:
        updates = (
            (CONFIG_RELATIVE_PATH, config_text, f"for {provider}"),
            (WORKFLOW_RELATIVE_PATH, workflow_text, f"to use {provider}"),
        )
        updated: list[Path] = []
        not_updated: list[Path] = []
        for relative_path, content, message in updates:
            try:
                self.options.write_text(self.path(relative_path), content)
            except OSError as exc:
                not_updated.append(relative_path)
                messages.print_write_failure(self.console, relative_path, str(exc))
                continue
            updated.append(relative_path)
            messages.print_write_success(self.console, relative_path, message)
        return WriteResult(tuple(updated), tuple(not_updated))

    def validate_provider_ready_setup(self, provider: str) -> None:
        messages.print_validation_header(self.console)
        try:
            config = load_config(self.path(CONFIG_RELATIVE_PATH))
            source = load_workflow_source_for_preflight(
                self.path(WORKFLOW_RELATIVE_PATH),
                project_root=self.options.project_root,
            )
            preview = workflow_runner.compile_workflow_preview(
                config=config,
                source=source,
                console=self.console,
                no_live=True,
                fingerprint_key_policy="read_only",
                project_root=self.options.project_root,
                state_dir=self.options.project_root / STATE_DIR_NAME,
                check_cli_availability=True,
                which_fn=self.options.which_fn,
                workspace_real_execution=False,
            )
            workflow_runner.raise_for_preflight_preview_errors(preview, self.console)
        except typer.Exit:
            messages.print_validation_failure(self.console, provider)
            raise
        except Exception as exc:
            messages.print_validation_exception(self.console, str(exc))
            messages.print_validation_failure(self.console, provider)
            raise typer.Exit(code=1) from exc
        messages.print_validation_success(self.console, len(preview.nodes))

    def confirm(self, prompt: str, default: bool) -> bool:
        default_answer = "y" if default else "n"
        answer = self.prompt_raw(prompt).strip().lower()
        if answer == "":
            answer = default_answer
        while answer not in {"y", "yes", "n", "no"}:
            messages.print_invalid_choice(self.console, ("y", "n"))
            answer = self.prompt_raw(prompt).strip().lower()
            if answer == "":
                answer = default_answer
        return answer in {"y", "yes"}

    def prompt_choice(self, prompt: str, choices: tuple[str, ...], default: str) -> str:
        answer = self.prompt_raw(prompt).strip()
        if answer == "":
            return default
        while answer not in choices:
            messages.print_invalid_choice(self.console, choices)
            answer = self.prompt_raw(prompt).strip()
            if answer == "":
                return default
        return answer

    def prompt_raw(self, prompt: str) -> str:
        messages.print_prompt(self.console, prompt)
        return self.options.read_input()


__all__ = [
    "CONFIG_RELATIVE_PATH",
    "ONBOARDING_COMMAND_HELP",
    "PROVIDER_SETUP_URL",
    "WORKFLOW_RELATIVE_PATH",
    "OnboardingOptions",
    "default_onboarding_options",
    "run_onboarding",
    "run_onboarding_command",
    "write_text_file",
]

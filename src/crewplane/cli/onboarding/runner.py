from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import typer
from rich.console import Console
from yaml import YAMLError

from crewplane.adapters.invokers.cli_invoker import (
    DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
    CliDiscoveryRequest,
    unconfirmed_process_cleanup,
)
from crewplane.core.config import AgentConfig, Config, load_config
from crewplane.core.preflight import (
    PreflightCompilationPreview,
    load_workflow_source_for_preflight,
)
from crewplane.core.preflight.diagnostics import PreflightDiagnosticCode
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.core.state_paths import STATE_DIR_NAME

from .. import workflow_runner
from ..run.preflight import uses_cli_invoker, uses_mock_invoker
from . import generated_files as _generated_files
from . import messages
from .constants import (
    CONFIG_RELATIVE_PATH,
    ONBOARDING_COMMAND_HELP,
    PROVIDER_SETUP_URL,
    WORKFLOW_RELATIVE_PATH,
)
from .detection import ProviderDetection, ProviderStatus, detect_providers
from .history import find_successful_mock_run_evidence
from .local_connection import configure_local_connection
from .plugin_setup import LocalSetupFailure, prepare_local_agent
from .rendering import rendered_default_config, rendered_default_workflow

TextWriter = Callable[[Path, str], None]
WhichFunction = Callable[[str], str | None]
ReadInput = Callable[[], str]
WriteResult = _generated_files.WriteResult


@dataclass(frozen=True)
class OnboardingOptions:
    """Caller-owned I/O and discovery settings for synchronous onboarding.

    project_root anchors project files and discovery; callers should supply an
    absolute project directory. No chdir or root normalization is performed.
    Both terminal flags must be true to prompt or mutate files. read_input
    returns one answer without a prompt; which_fn resolves an executable name
    or returns None. write_text writes supplied text to the supplied path.
    Callbacks run sequentially and retain ownership of their resources;
    interruptions and unhandled callback failures propagate from run_onboarding.
    """

    project_root: Path
    console: Console
    input_is_terminal: bool
    output_is_terminal: bool
    read_input: ReadInput
    which_fn: WhichFunction
    write_text: TextWriter
    discovery_timeout_seconds: float = DEFAULT_DISCOVERY_TIMEOUT_SECONDS


@dataclass(frozen=True)
class OnboardingProjectState:
    config: Config
    workflow_path: Path
    default_config: str
    default_workflow: str


def run_onboarding_command(
    discovery_timeout_seconds: float = DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
) -> None:
    """Run onboarding in the resolved cwd using process streams and filesystem I/O.

    Apply discovery_timeout_seconds to local-provider discovery. Filesystem and
    failure behavior follow run_onboarding; unhandled failures propagate.
    """
    options = replace(
        default_onboarding_options(Console()),
        discovery_timeout_seconds=discovery_timeout_seconds,
    )
    run_onboarding(options)


def default_onboarding_options(console: Console) -> OnboardingOptions:
    """Capture the resolved cwd and terminal flags without changing project files.

    Use input, shutil.which, and UTF-8 writes with the caller-owned console.
    Resolving the cwd can raise OSError; callbacks are invoked only during a run.
    """
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
    return input()


def write_text_file(path: Path, content: str) -> None:
    """Create or overwrite path with UTF-8 text, closing the file before returning.

    Relative paths use the cwd; parent directories must already exist. Writes
    are non-atomic, without backups; filesystem and encoding errors propagate.
    """
    path.write_text(content, encoding="utf-8")


def run_onboarding(options: OnboardingOptions) -> None:
    """Interactively prepare provider settings under options.project_root.

    Return None on completion or a handled stop. Accepted initialization creates
    missing templates without overwriting files. Provider handoff rechecks the
    defaults, then writes config before workflow, independently and without
    backups. OSError writes are reported and the next file is still attempted.
    Validation may probe Ollama after saving; failure retains saved files unless
    the user accepts recovery. Existing CLI setups may update their Ollama
    connection. Handled validation failures raise typer.Exit; prompt interrupts,
    unconfirmed process cleanup, and other unhandled I/O/callback errors propagate.
    """
    OnboardingRunner(options).run()


class ProviderReadinessFailure(typer.Exit):
    """Provider checks failed after settings were saved."""


class OnboardingRunner:
    def __init__(self, options: OnboardingOptions) -> None:
        self.options = options
        self.console = options.console
        self.local_agent: AgentConfig | None = None
        self.provider_choices: tuple[ProviderDetection, ...] = ()
        self.files = _generated_files.GeneratedOnboardingFiles(options, self.confirm)

    def run(self) -> None:
        messages.print_title(self.console)
        project_state = self.load_project_state()
        if project_state is None:
            return
        providers = self.choose_onboarding_providers(project_state)
        if not providers:
            return
        providers = self.prepare_selected_providers(providers)
        if not providers or not self.apply_provider_handoff(providers, project_state):
            return
        self.finish_onboarding(providers, project_state)

    def load_project_state(self) -> OnboardingProjectState | None:
        if not self.is_interactive():
            messages.print_non_tty_stop(self.console)
            return None
        if self.configure_existing_cli_setup():
            return None
        if not self.files.ensure_generated_files():
            return None

        config = self.load_config_for_onboarding()
        if config is None:
            return None

        default_config = rendered_default_config()
        default_workflow = rendered_default_workflow()
        if not self.config_can_be_onboarded(config, default_workflow):
            return None
        if not self.files.generated_files_are_safe_to_update(
            default_config, default_workflow
        ):
            return None

        return OnboardingProjectState(
            config=config,
            workflow_path=self.path(WORKFLOW_RELATIVE_PATH),
            default_config=default_config,
            default_workflow=default_workflow,
        )

    def configure_existing_cli_setup(self) -> bool:
        if not self.path(CONFIG_RELATIVE_PATH).is_file():
            return False
        try:
            config = load_config(self.path(CONFIG_RELATIVE_PATH))
        except (OSError, ValueError, TypeError, YAMLError):
            # The initialization path reports invalid configs.
            return False
        if not uses_cli_invoker(config):
            return False
        if not configure_local_connection(self.options, self.prompt_raw, self.confirm):
            self.print_existing_cli_config_stop(rendered_default_workflow())
        return True

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
        if self.files.workflow_matches_default(default_workflow):
            messages.print_partial_update(
                self.console,
                (CONFIG_RELATIVE_PATH,),
                (WORKFLOW_RELATIVE_PATH,),
            )
            return
        messages.print_already_onboarded(self.console)

    def choose_onboarding_providers(
        self, project_state: OnboardingProjectState
    ) -> tuple[str, ...] | None:
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

        providers = self.choose_providers(found)
        messages.print_provider_selected(self.console, ", ".join(providers))
        return providers

    def found_provider_detections(self) -> tuple[ProviderDetection, ...]:
        detections = self.detect_providers()
        self.provider_choices = tuple(item for item in detections if item.available)
        plugin_missing = any(
            item.status == ProviderStatus.PLUGIN_MISSING for item in detections
        )
        if plugin_missing and not self.continue_without_ollama(installation=True):
            return ()
        if not self.provider_choices:
            messages.print_no_providers_stop(self.console)
        return self.provider_choices

    def continue_without_ollama(self, installation: bool = False) -> bool:
        messages.print_ollama_choices(self.console, installation)
        if (
            self.prompt_choice(messages.OPTIONAL_PROVIDER_PROMPT, ("1", "2"), "1")
            == "1"
        ):
            return True
        if installation:
            messages.print_ollama_setup_link(self.console)
        return False

    def prepare_selected_providers(self, providers: tuple[str, ...]) -> tuple[str, ...]:
        if "local" not in providers:
            return providers
        result = self.prepare_local_provider()
        if isinstance(result, AgentConfig):
            self.local_agent = result
            self.console.print(
                f"Selected Ollama model: {result.default_model}", markup=False
            )
            return providers
        messages.print_ollama_unconfigured(
            self.console, result == LocalSetupFailure.CANCELLED
        )
        if not self.continue_without_ollama():
            return ()
        return self.choose_without_ollama(providers)

    def prepare_local_provider(self) -> AgentConfig | LocalSetupFailure:
        return prepare_local_agent(
            CliDiscoveryRequest(
                self.options.project_root,
                os.environ.copy(),
                self.options.which_fn,
                self.options.discovery_timeout_seconds,
            ),
            self.console,
            self.prompt_raw,
        )

    def choose_without_ollama(self, providers: tuple[str, ...]) -> tuple[str, ...]:
        self.local_agent = None
        self.provider_choices = tuple(
            item for item in self.provider_choices if item.provider != "local"
        )
        remaining = tuple(provider for provider in providers if provider != "local")
        if remaining:
            return remaining
        if self.provider_choices:
            return self.choose_providers(self.provider_choices)
        messages.print_no_providers_stop(self.console)
        return ()

    def apply_provider_handoff(
        self, providers: tuple[str, ...], project_state: OnboardingProjectState
    ) -> bool:
        if not self.confirm_file_preparation(", ".join(providers)):
            messages.print_declined_changes(self.console)
            return False
        write_result = self.files.prepare_files(
            providers, project_state, self.local_agent
        )
        return self.files.write_result_is_complete(write_result)

    def is_interactive(self) -> bool:
        return self.options.input_is_terminal and self.options.output_is_terminal

    def path(self, relative_path: Path) -> Path:
        return self.options.project_root / relative_path

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
        detections = detect_providers(
            rendered_default_config(), self.options.project_root, self.options.which_fn
        )
        messages.print_provider_detection(self.console, detections)
        return detections

    def choose_providers(
        self, detections: tuple[ProviderDetection, ...]
    ) -> tuple[str, ...]:
        providers = tuple(detection.provider for detection in detections)
        messages.print_provider_choices(self.console, providers)
        choices = tuple(str(index) for index in range(1, len(providers) + 1))
        while True:
            answer = self.prompt_raw(messages.PROVIDER_CHOICE_PROMPT)
            selections = tuple(dict.fromkeys(answer.replace(",", " ").split()))
            if selections and all(selection in choices for selection in selections):
                return tuple(providers[int(selection) - 1] for selection in selections)
            messages.print_invalid_provider_selection(self.console)

    def confirm_file_preparation(self, provider: str) -> bool:
        messages.print_file_preparation_confirmation(self.console, provider)
        return self.confirm(messages.APPLY_CHANGES_PROMPT, True)

    def finish_onboarding(
        self, providers: tuple[str, ...], project_state: OnboardingProjectState
    ) -> None:
        try:
            self.validate_provider_ready_setup(", ".join(providers))
        except ProviderReadinessFailure:
            if "local" not in providers or not self.continue_without_ollama():
                raise
            providers = self.choose_without_ollama(providers)
            if not self.files.replace_saved_setup(providers, project_state):
                raise
            if not providers:
                self.console.print(
                    "Original mock settings restored. No real provider was configured."
                )
                return
            self.validate_provider_ready_setup(", ".join(providers))
        messages.print_final_success(self.console, ", ".join(providers))

    def validate_provider_ready_setup(self, provider: str) -> None:
        messages.print_validation_header(self.console)
        provider_checks_failed = False
        try:
            preview = self.compile_provider_ready_preview()
            provider_checks_failed = self.only_provider_checks_failed(preview)
            workflow_runner.raise_for_preflight_preview_errors(preview, self.console)
        except typer.Exit as exc:
            messages.print_validation_failure(self.console, provider)
            if provider_checks_failed:
                raise ProviderReadinessFailure(code=1) from exc
            raise
        except Exception as exc:
            if unconfirmed_process_cleanup(exc) is not None:
                raise
            messages.print_validation_exception(self.console, str(exc))
            messages.print_validation_failure(self.console, provider)
            raise typer.Exit(code=1) from exc
        messages.print_validation_success(self.console, len(preview.nodes))

    def compile_provider_ready_preview(self) -> PreflightCompilationPreview:
        config = load_config(self.path(CONFIG_RELATIVE_PATH))
        source = load_workflow_source_for_preflight(
            self.path(WORKFLOW_RELATIVE_PATH),
            project_root=self.options.project_root,
        )
        return workflow_runner.compile_workflow_preview(
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

    @staticmethod
    def only_provider_checks_failed(preview: PreflightCompilationPreview) -> bool:
        errors = [item for item in preview.diagnostics if item.severity == "error"]
        return bool(errors) and all(
            item.code == PreflightDiagnosticCode.PROVIDER_CLI for item in errors
        )

    def confirm(self, prompt: str, default: bool) -> bool:
        while True:
            answer = self.prompt_raw(prompt).strip().lower()
            if answer == "":
                return default
            if answer in {"y", "yes", "n", "no"}:
                return answer in {"y", "yes"}
            messages.print_invalid_choice(self.console, ("y", "n"))

    def prompt_choice(self, prompt: str, choices: tuple[str, ...], default: str) -> str:
        while True:
            answer = self.prompt_raw(prompt).strip()
            if answer == "":
                return default
            if answer in choices:
                return answer
            messages.print_invalid_choice(self.console, choices)

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

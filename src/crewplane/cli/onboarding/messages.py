from __future__ import annotations

from pathlib import Path

from rich.console import Console

from .constants import (
    CONFIG_RELATIVE_PATH,
    PROVIDER_SETUP_URL,
    WORKFLOW_RELATIVE_PATH,
)
from .detection import ProviderDetection
from .rendering import (
    manual_config_snippet,
    manual_workflow_snippet,
    rendered_default_config,
    rendered_default_workflow,
)

INIT_RECOVERY_PROMPT = "Run non-overwriting crewplane init now? [y/N]: "
MISSING_MOCK_EVIDENCE_PROMPT = "Choose [0]: "
PROVIDER_CHOICE_PROMPT = "Choose one or more providers (comma-separated numbers): "
APPLY_CHANGES_PROMPT = "Apply changes? [Y/n]: "
OPTIONAL_PROVIDER_PROMPT = "Choose [1]: "


def print_title(console: Console) -> None:
    console.print("")
    console.print("[bold]Crewplane onboarding[/]")


def print_onboarding_intro(console: Console) -> None:
    console.print("")
    console.print(
        "Onboarding connects real provider CLIs to your Crewplane setup. "
        "It detects which providers are on your PATH, lets you pick one or more, and "
        "updates the generated config and workflow — no manual YAML editing."
    )
    console.print("")
    console.print("Run these first if you haven't already:")
    console.print("  [cyan]crewplane init[/]")
    console.print("  [cyan]crewplane validate[/]")
    console.print("  [cyan]crewplane run[/]")
    console.print("")
    console.print(
        "Selecting Ollama loads the official Ollama plugin to discover servers and models."
    )
    console.print(
        "Applying Ollama setup runs a short readiness prompt. Crewplane does not"
    )
    console.print(
        "install software, start servers, authenticate providers, or overwrite edited files."
    )


def print_non_tty_stop(console: Console) -> None:
    console.print("")
    console.print("Onboarding is interactive.")
    console.print("No prompts were shown and no files were changed.")
    console.print("")
    console.print("Run this command in a terminal:")
    console.print("  [cyan]crewplane onboarding[/]")


def print_missing_state_dir(console: Console, project_root: Path) -> None:
    console.print("")
    console.print("This directory does not look initialized for Crewplane.")
    console.print(f"Current directory: {project_root}")
    console.print("")


def print_missing_generated_files(console: Console) -> None:
    console.print("")
    console.print("Generated Crewplane files are missing.")
    console.print("crewplane init can create missing generated files.")
    console.print("Existing files will not be overwritten.")
    console.print("")


def print_no_files_changed(console: Console) -> None:
    console.print("No files changed.")


def print_already_onboarded(console: Console) -> None:
    console.print("")
    console.print("[bold]Existing real-provider Crewplane config detected.[/]")
    console.print("")
    console.print(
        "This project is already configured to start real provider CLIs "
        "through crewplane run."
    )
    console.print("Onboarding will not rewrite it.")
    console.print("")
    console.print("Use:")
    console.print("  [cyan]crewplane validate[/]")
    console.print("  [cyan]crewplane run[/]")


def print_user_owned_stop(console: Console, reason: str) -> None:
    console.print("")
    console.print("Onboarding will not rewrite this Crewplane setup.")
    console.print(reason)
    print_provider_setup_link(console, "Use the provider setup guide:")


def print_edited_files_stop(console: Console) -> None:
    console.print("")
    console.print("Crewplane found local edits in the generated files.")
    console.print("")
    console.print("Onboarding will not overwrite edited files in v1.")
    print_provider_setup_link(
        console,
        "Use the provider setup guide for customized Crewplane files:",
    )


def print_no_providers_stop(console: Console) -> None:
    console.print("")
    console.print("No providers are available to set up.")
    console.print("Resolve the issues listed above, then rerun:")
    console.print("  [cyan]crewplane onboarding[/]")
    print_provider_setup_link(console, "Provider setup guide:")


def print_declined_changes(console: Console) -> None:
    print_no_files_changed(console)
    print_provider_setup_link(console, "Manual setup guide:")


def print_provider_selected(console: Console, provider: str) -> None:
    console.print("")
    console.print(f"Selected {provider_display_name(provider)}.")


def print_quickstart_state(console: Console) -> None:
    console.print("")
    console.print("[bold]1. Quickstart state[/]")
    console.print(f"   [green]✓[/] {CONFIG_RELATIVE_PATH} exists")
    console.print(f"   [green]✓[/] {WORKFLOW_RELATIVE_PATH} exists")


def print_mock_run_evidence_found(console: Console) -> None:
    console.print("   [green]✓[/] Successful provider-free run manifest found")
    console.print("   [green]✓[/] Generated mock setup detected")


def print_mock_run_evidence_warning(console: Console, warning: str) -> None:
    console.print(f"   [yellow]WARN[/] {warning}")


def print_missing_mock_run_evidence(console: Console) -> None:
    console.print(
        "   [yellow]WARN[/] No successful provider-free mock run manifest found."
    )
    console.print("")
    console.print("The README path normally runs [cyan]crewplane run[/] first.")
    console.print("[1] continue real-provider setup anyway")
    console.print("[0] quit and run mock first")


def provider_display_name(provider: str) -> str:
    return ", ".join(
        "Ollama" if name == "local" else name for name in provider.split(", ")
    )


def print_provider_diagnostic(console: Console, provider: str, diagnostic: str) -> None:
    console.print(f"   {provider_display_name(provider)}: {diagnostic}", markup=False)


def print_ollama_choices(console: Console, installation: bool = False) -> None:
    console.print("")
    console.print("Ollama is optional. How would you like to proceed?")
    console.print("  [1] Continue onboarding without Ollama", markup=False)
    action = (
        "Quit and show plugin installation instructions" if installation else "Quit"
    )
    console.print(f"  [2] {action}", markup=False)


def print_ollama_setup_link(console: Console) -> None:
    console.print(
        "Install crewplane-ollama in the Python environment running Crewplane."
    )
    print_provider_setup_link(
        console, "Ollama installation:", "local-ollama-text-generation"
    )


def print_ollama_unconfigured(console: Console, cancelled: bool = False) -> None:
    detail = (
        "Ollama setup was cancelled." if cancelled else "Ollama setup could not finish."
    )
    console.print(detail)
    console.print("Ollama was not configured.")
    print_no_files_changed(console)


def print_recovery_confirmation(console: Console, provider: str) -> None:
    if provider:
        console.print(
            f"Replace the settings saved by this attempt to use {provider_display_name(provider)}."
        )
    else:
        console.print("Restore the original mock settings saved before this attempt.")
    console.print("Files edited since onboarding saved them will be left unchanged.")


def print_provider_detection(
    console: Console, detections: tuple[ProviderDetection, ...]
) -> None:
    console.print("")
    console.print("[bold]2. Provider detection[/]")
    for detection in detections:
        name = provider_display_name(detection.provider)
        console.print(f"   {name}: {detection.status.value}", markup=False)
        if detection.diagnostic:
            print_provider_diagnostic(console, name, detection.diagnostic)
    console.print("")
    console.print(
        "These checks find setup options; they do not mean a provider is configured."
    )


def print_provider_choices(console: Console, providers: tuple[str, ...]) -> None:
    console.print("")
    console.print("[bold]3. Provider handoff[/]")
    console.print("")
    console.print("Crewplane found these provider options:")
    console.print("")
    for index, provider in enumerate(providers, start=1):
        console.print(f"  [{index}] {provider_display_name(provider)}")
    console.print("Select at least one provider; you can select all listed providers.")


def print_file_preparation_confirmation(console: Console, provider: str) -> None:
    console.print("")
    console.print("Onboarding will update unchanged generated defaults:")
    console.print(f"  {CONFIG_RELATIVE_PATH}")
    console.print(f"  {WORKFLOW_RELATIVE_PATH}")
    console.print("")
    console.print(
        "No backup files will be written because onboarding only updates "
        "unchanged generated defaults."
    )
    console.print("")
    if "local" in provider.split(", "):
        console.print(
            "Ollama readiness will run 'ollama show' and a short 'Reply with OK only.' inference prompt after saving. This may load the model and use your GPU."
        )
        console.print(
            "Ollama provides text generation from supplied content, without file or tool access."
        )
    else:
        console.print(
            f"It will not start or authenticate {provider}, or verify account/model access."
        )
    console.print(
        "The generated profiles include configured provider permissions; review"
    )
    console.print(
        ".crewplane/config.yml before running if you want different settings."
    )
    console.print("")
    print_provider_setup_link(console, "Provider setup details:")
    console.print("")


def print_write_failure(console: Console, relative_path: Path, error: str) -> None:
    console.print(f"[red]✗[/] Could not update {relative_path}: {error}")


def print_write_success(console: Console, relative_path: Path, message: str) -> None:
    console.print(f"[green]✓[/] Updated {relative_path} {message}")


def print_validation_header(console: Console) -> None:
    console.print("")
    console.print("[bold]4. Validate provider-ready setup[/]")


def print_validation_exception(console: Console, error: str) -> None:
    console.print(f"[red]Validation failed:[/] {error}")


def print_validation_success(console: Console, node_count: int) -> None:
    console.print("   [green]✓[/] Workflow parsed")
    console.print(f"   [green]✓[/] {node_count} node compiled")
    console.print("   [green]✓[/] Providers resolved")
    console.print("   [green]✓[/] Preflight plan compiled")
    console.print("   [green]✓[/] Provider executable found on PATH")


def print_invalid_choice(console: Console, choices: tuple[str, ...]) -> None:
    console.print(f"Choose one of: {', '.join(choices)}")


def print_invalid_provider_selection(console: Console) -> None:
    console.print("Select at least one provider using the listed numbers.")


def print_prompt(console: Console, prompt: str) -> None:
    console.print(prompt, end="", markup=False)


def print_manual_fallback(
    console: Console, providers: tuple[str, ...], reason: str
) -> None:
    console.print("")
    console.print("Crewplane cannot safely update generated files.")
    console.print(reason)
    console.print("")
    console.print(
        f"Manual {provider_display_name(', '.join(providers))} config snippet:"
    )
    console.print("```yaml")
    console.print(manual_config_snippet(rendered_default_config(), providers))
    console.print("```")
    console.print("")
    console.print(
        "Do not leave mock options such as output_mode, seed, delay_seconds, "
        "or observation_delay_seconds under the cli invoker."
    )
    console.print(
        "The cli invoker uses options: {} for built-ins, or explicit providers registrations for plugins."
    )
    console.print("")
    console.print("Workflow provider switch:")
    console.print("```yaml")
    console.print(manual_workflow_snippet(rendered_default_workflow(), providers))
    console.print("```")
    print_provider_setup_link(console, "Provider setup guide:")


def print_partial_update(
    console: Console, updated: tuple[Path, ...], not_updated: tuple[Path, ...]
) -> None:
    console.print("")
    console.print("[yellow]⚠ Partial onboarding update.[/]")
    console.print("")
    print_path_list(console, "Updated:", updated)
    print_path_list(console, "Not updated:", not_updated)
    console.print("")
    console.print(
        "This project may be in an inconsistent Crewplane state because "
        "only one generated file was updated."
    )
    print_provider_setup_link(
        console,
        "Use the provider setup guide to repair the config/workflow pair:",
    )


def print_no_files_updated(console: Console) -> None:
    console.print("")
    console.print("No onboarding files were updated.")
    print_provider_setup_link(console, "Use the provider setup guide for manual setup:")


def print_validation_failure(console: Console, provider: str) -> None:
    console.print("")
    console.print(
        f"Saved settings for {provider_display_name(provider)} did not pass validation."
    )
    console.print("Files were not rolled back.")
    print_provider_setup_link(console, "Use the provider setup guide to repair setup:")


def print_final_success(console: Console, provider: str) -> None:
    console.print("")
    console.print("[green]Onboarding complete.[/]")
    console.print("")
    console.print(
        f"Crewplane config is ready to start {provider_display_name(provider)} when you run the workflow."
    )
    console.print("")
    console.print(f"Onboarding configured: {provider_display_name(provider)}.")
    print_provider_setup_link(console, "To add more providers, use:")
    console.print("")
    console.print("Recommended first real run:")
    console.print("  [cyan]crewplane run[/]")
    console.print("")
    console.print("Run without the live dashboard:")
    console.print("  [cyan]crewplane run --no-live[/]")
    console.print("")
    if "local" in provider.split(", "):
        console.print("Ollama is configured and passed the readiness check.")
        console.print(
            "Text-generation example: crewplane run --no-live --tasks .crewplane/workflows/example-templates/text-generation-example.task.md"
        )
        console.print(
            "If a WSL host address changes, update agents.local.environment.OLLAMA_HOST in config.yml and run crewplane validate. Other providers' credentials and account access are not checked."
        )
    else:
        console.print(
            f"Crewplane has not checked {provider.capitalize()} auth, account status, model access, or provider settings."
        )
        console.print(
            "If the provider CLI prompts, fails auth, or lacks model access, fix that in the provider tool and rerun."
        )


def print_provider_setup_link(
    console: Console, heading: str, section: str = ""
) -> None:
    console.print("")
    console.print(heading)
    url = f"{PROVIDER_SETUP_URL}#{section}" if section else PROVIDER_SETUP_URL
    console.print(f"  {url}", markup=False, soft_wrap=True)


def print_path_list(console: Console, heading: str, paths: tuple[Path, ...]) -> None:
    console.print(heading)
    for path in paths:
        console.print(f"  {path}")


__all__ = [
    "APPLY_CHANGES_PROMPT",
    "INIT_RECOVERY_PROMPT",
    "MISSING_MOCK_EVIDENCE_PROMPT",
    "PROVIDER_CHOICE_PROMPT",
    "OPTIONAL_PROVIDER_PROMPT",
    "provider_display_name",
    "print_provider_diagnostic",
    "print_ollama_choices",
    "print_ollama_setup_link",
    "print_ollama_unconfigured",
    "print_recovery_confirmation",
    "print_already_onboarded",
    "print_declined_changes",
    "print_edited_files_stop",
    "print_file_preparation_confirmation",
    "print_final_success",
    "print_invalid_choice",
    "print_manual_fallback",
    "print_missing_generated_files",
    "print_missing_mock_run_evidence",
    "print_missing_state_dir",
    "print_mock_run_evidence_found",
    "print_mock_run_evidence_warning",
    "print_no_providers_stop",
    "print_no_files_changed",
    "print_no_files_updated",
    "print_non_tty_stop",
    "print_onboarding_intro",
    "print_partial_update",
    "print_prompt",
    "print_provider_choices",
    "print_provider_detection",
    "print_provider_selected",
    "print_invalid_provider_selection",
    "print_quickstart_state",
    "print_title",
    "print_user_owned_stop",
    "print_validation_exception",
    "print_validation_failure",
    "print_validation_header",
    "print_validation_success",
    "print_write_failure",
    "print_write_success",
]

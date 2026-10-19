from __future__ import annotations

import io
from collections import deque
from dataclasses import replace
from importlib import metadata
from pathlib import Path
from unittest.mock import Mock

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

import crewplane.cli.app as cli
from crewplane.adapters.invokers.cli import CliInvokerAdapter
from crewplane.architecture.loader import resolve_implementation_path
from crewplane.artifacts.naming import build_run_key_name
from crewplane.cli import workflow_runner
from crewplane.cli.onboarding import (
    CONFIG_RELATIVE_PATH,
    ONBOARDING_COMMAND_HELP,
    WORKFLOW_RELATIVE_PATH,
    OnboardingOptions,
    run_onboarding,
    write_text_file,
)
from crewplane.cli.onboarding.generated_files import GeneratedOnboardingFiles
from crewplane.cli.onboarding.rendering import (
    render_provider_ready_config,
    render_provider_ready_workflow,
    rendered_default_config,
    rendered_default_workflow,
)
from crewplane.cli.onboarding.runner import (
    OnboardingRunner,
    ProviderReadinessFailure,
    read_stdin_line,
)
from crewplane.cli.project_init import initialize_project_templates
from crewplane.cli.run.preflight import (
    compile_workflow_preview,
    raise_for_preflight_preview_errors,
)
from crewplane.core.config import load_config
from crewplane.core.preflight import (
    PreflightCompilationPreview,
    load_workflow_source_for_preflight,
)
from crewplane.core.preflight.diagnostics import (
    PreflightDiagnostic,
    PreflightDiagnosticCode,
    PreflightDiagnosticPhase,
)
from crewplane.core.provider_names import known_provider_names
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import make_run_manifest, write_run_manifest

pytestmark = pytest.mark.usefixtures("posix_cli_plans")


class WhichRecorder:
    def __init__(self, found: dict[str, str] | None = None) -> None:
        self.found = found or {}
        self.calls: list[str] = []

    def __call__(self, executable_name: str) -> str | None:
        self.calls.append(executable_name)
        return self.found.get(executable_name)


class DetectionOnlyCodex:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, executable_name: str) -> str | None:
        self.calls.append(executable_name)
        if executable_name == "codex" and self.calls.count("codex") == 1:
            return "/usr/bin/codex"
        return None


@pytest.fixture(autouse=True)
def missing_ollama_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    original_version = metadata.version

    def installed_version(distribution: str) -> str:
        if distribution == "crewplane-ollama":
            raise metadata.PackageNotFoundError(distribution)
        return original_version(distribution)

    monkeypatch.setattr(metadata, "version", installed_version)


@pytest.mark.parametrize("answers", [["1"], [""], ["invalid", "3", "1"]])
def test_missing_ollama_plugin_can_continue_with_codex(
    tmp_path: Path, answers: list[str]
) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)
    output, _ = run_onboarding_in_project(
        tmp_path,
        answers=[*answers, "1", ""],
        which=WhichRecorder({"codex": "/usr/bin/codex", "ollama": "/usr/bin/ollama"}),
    )

    config = load_config(tmp_path / CONFIG_RELATIVE_PATH)
    assert set(config.agents) == {"codex"}
    assert config.settings.integrations.invoker.options == {}
    assert "Continue onboarding without Ollama" in output
    assert "crewplane-ollama" in output
    assert "not installed" in output
    assert "Onboarding complete." in output
    assert "local   not found" not in output


def test_missing_ollama_plugin_can_quit_for_installation(tmp_path: Path) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)
    before = {
        path: (tmp_path / path).read_bytes()
        for path in (CONFIG_RELATIVE_PATH, WORKFLOW_RELATIVE_PATH)
    }

    output, _ = run_onboarding_in_project(
        tmp_path,
        answers=["2"],
        which=WhichRecorder({"codex": "/usr/bin/codex"}),
    )

    assert "crewplane-ollama" in output
    assert "provider-setup.md#local-ollama-text-generation" in output
    assert "Onboarding complete" not in output
    assert all(
        (tmp_path / path).read_bytes() == content for path, content in before.items()
    )


def test_root_help_lists_onboarding_command() -> None:
    result = CliRunner().invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "onboarding" in result.output
    assert ONBOARDING_COMMAND_HELP in result.output


def test_cli_onboarding_non_tty_does_not_prompt_or_mutate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.app, ["onboarding"])

    assert result.exit_code == 0
    assert "Onboarding is interactive." in result.output
    assert "crewplane onboarding" in result.output
    assert not (tmp_path / ".crewplane").exists()


def test_missing_state_dir_defaults_to_no_init(tmp_path: Path) -> None:
    output, _ = run_onboarding_in_project(tmp_path, answers=[""])

    assert "Current directory:" in output
    assert "Run non-overwriting crewplane init now? [y/N]: " in output
    assert "No files changed." in output
    assert not (tmp_path / ".crewplane").exists()


def test_confirmation_prompt_rejects_invalid_answers(tmp_path: Path) -> None:
    output, _ = run_onboarding_in_project(tmp_path, answers=["maybe", ""])

    assert "Run non-overwriting crewplane init now? [y/N]: " in output
    assert "Choose one of: y, n" in output
    assert "No files changed." in output
    assert not (tmp_path / ".crewplane").exists()


def test_missing_generated_files_offer_non_overwriting_init(tmp_path: Path) -> None:
    (tmp_path / ".crewplane").mkdir()

    output, _ = run_onboarding_in_project(tmp_path, answers=["y", ""])

    assert "Generated Crewplane files are missing." in output
    assert (tmp_path / CONFIG_RELATIVE_PATH).is_file()
    assert (tmp_path / WORKFLOW_RELATIVE_PATH).is_file()
    assert "No successful provider-free mock run manifest found" in output


def test_missing_generated_files_do_not_overwrite_existing_config(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".crewplane"
    state_dir.mkdir()
    config_path = state_dir / "config.yml"
    config_path.write_text("not: valid: yaml\n", encoding="utf-8")

    output, _ = run_onboarding_in_project(tmp_path, answers=["y"])

    assert config_path.read_text(encoding="utf-8") == "not: valid: yaml\n"
    assert "Config could not be loaded" in output
    assert (tmp_path / WORKFLOW_RELATIVE_PATH).is_file()


def test_already_cli_config_is_reported_unchanged(tmp_path: Path) -> None:
    initialize_default_project(tmp_path)
    config_path = tmp_path / CONFIG_RELATIVE_PATH
    workflow_path = tmp_path / WORKFLOW_RELATIVE_PATH
    config_text = render_provider_ready_config(rendered_default_config(), ("codex",))
    workflow_text = render_provider_ready_workflow(
        rendered_default_workflow(), ("codex",)
    )
    config_path.write_text(config_text, encoding="utf-8")
    workflow_path.write_text(workflow_text, encoding="utf-8")

    output, which = run_onboarding_in_project(tmp_path)

    assert "Crewplane onboarding" in output
    assert "Onboarding connects real provider CLIs" not in output
    assert "Run these first if you haven't already:" not in output
    assert "Existing real-provider Crewplane config detected." in output
    assert config_path.read_text(encoding="utf-8") == config_text
    assert workflow_path.read_text(encoding="utf-8") == workflow_text
    assert which.calls == []


def test_config_only_cli_state_is_reported_as_partial_on_rerun(
    tmp_path: Path,
) -> None:
    initialize_default_project(tmp_path)
    config_path = tmp_path / CONFIG_RELATIVE_PATH
    workflow_path = tmp_path / WORKFLOW_RELATIVE_PATH
    config_text = render_provider_ready_config(rendered_default_config(), ("codex",))
    workflow_text = workflow_path.read_text(encoding="utf-8")
    config_path.write_text(config_text, encoding="utf-8", newline="\n")

    output, which = run_onboarding_in_project(tmp_path)

    assert "Partial onboarding update." in output
    assert "Existing real-provider Crewplane config detected." not in output
    assert "Updated:\n  .crewplane/config.yml" in output
    assert "Not updated:\n  .crewplane/workflows/single-agent-review.task.md" in output
    assert "Use the provider setup guide to repair the config/workflow pair" in output
    assert config_path.read_text(encoding="utf-8") == config_text
    assert workflow_path.read_text(encoding="utf-8") == workflow_text
    assert which.calls == []


def test_edited_generated_files_are_left_untouched(tmp_path: Path) -> None:
    initialize_default_project(tmp_path)
    workflow_path = tmp_path / WORKFLOW_RELATIVE_PATH
    workflow_text = workflow_path.read_text(encoding="utf-8") + "\nLocal edit.\n"
    workflow_path.write_text(workflow_text, encoding="utf-8")

    output, which = run_onboarding_in_project(tmp_path)

    assert "Crewplane onboarding" in output
    assert "Onboarding connects real provider CLIs" not in output
    assert "Run these first if you haven't already:" not in output
    assert "local edits" in output
    assert workflow_path.read_text(encoding="utf-8") == workflow_text
    assert which.calls == []


def test_generated_file_read_failure_stops_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)
    config_path = tmp_path / CONFIG_RELATIVE_PATH
    default_config = config_path.read_text(encoding="utf-8")
    original_generated_files_exist = GeneratedOnboardingFiles.generated_files_exist

    def generated_files_exist_then_remove_workflow(
        files: GeneratedOnboardingFiles,
    ) -> bool:
        generated_files_exist = original_generated_files_exist(files)
        if generated_files_exist:
            (tmp_path / WORKFLOW_RELATIVE_PATH).unlink()
        return generated_files_exist

    monkeypatch.setattr(
        GeneratedOnboardingFiles,
        "generated_files_exist",
        generated_files_exist_then_remove_workflow,
    )

    output, which = run_onboarding_in_project(
        tmp_path,
        which=WhichRecorder({"codex": "/usr/bin/codex"}),
    )

    assert "Onboarding cannot confirm generated files are unchanged" in output
    assert "Onboarding will not rewrite this Crewplane setup." in output
    assert "provider-setup.md" in output
    assert "Provider detection" not in output
    assert config_path.read_text(encoding="utf-8") == default_config
    assert which.calls == []


def test_user_owned_invoker_config_is_left_untouched(tmp_path: Path) -> None:
    initialize_default_project(tmp_path)
    config_path = tmp_path / CONFIG_RELATIVE_PATH
    config_text = config_path.read_text(encoding="utf-8").replace(
        '      implementation: "mock"',
        '      implementation: "tests.fake:Invoker"',
        1,
    )
    config_path.write_text(config_text, encoding="utf-8")

    output, which = run_onboarding_in_project(tmp_path)

    assert "Active invoker is not the generated mock setup." in output
    assert config_path.read_text(encoding="utf-8") == config_text
    assert which.calls == []


def test_missing_mock_evidence_defaults_to_quit(tmp_path: Path) -> None:
    initialize_default_project(tmp_path)
    default_config = (tmp_path / CONFIG_RELATIVE_PATH).read_text(encoding="utf-8")

    output, which = run_onboarding_in_project(
        tmp_path,
        answers=[""],
        which=WhichRecorder({"codex": "/usr/bin/codex"}),
    )

    assert "No successful provider-free mock run manifest found" in output
    assert "Provider detection" not in output
    assert (tmp_path / CONFIG_RELATIVE_PATH).read_text(
        encoding="utf-8"
    ) == default_config
    assert which.calls == []


def test_missing_mock_evidence_can_continue_and_select_provider(tmp_path: Path) -> None:
    initialize_default_project(tmp_path)

    output, which = run_onboarding_in_project(
        tmp_path,
        answers=["1", "1", "1", ""],
        which=WhichRecorder({"codex": "/usr/bin/codex"}),
    )

    assert "Provider detection" in output
    assert "Onboarding complete." in output
    assert which.calls == [*known_provider_names()[:-2], "env", "opencode", "codex"]


def test_no_provider_found_stops_with_setup_guidance(tmp_path: Path) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)
    paths = [tmp_path / CONFIG_RELATIVE_PATH, tmp_path / WORKFLOW_RELATIVE_PATH]
    before = [path.read_bytes() for path in paths]

    output, which = run_onboarding_in_project(
        tmp_path, answers=["1"], which=WhichRecorder()
    )

    assert "No providers are available to set up." in output
    assert "provider-setup.md" in output
    assert "Onboarding complete." not in output
    assert [path.read_bytes() for path in paths] == before
    assert sorted(which.calls) == sorted(
        [*known_provider_names()[:-2], "env", "opencode"]
    )


def test_successful_onboarding_selects_provider_writes_and_validates(
    tmp_path: Path,
) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)
    which = WhichRecorder({"codex": "/usr/bin/codex", "gemini": "/usr/bin/gemini"})

    output, _ = run_onboarding_in_project(tmp_path, answers=["1", "2", ""], which=which)

    config_text = (tmp_path / CONFIG_RELATIVE_PATH).read_text(encoding="utf-8")
    workflow_text = (tmp_path / WORKFLOW_RELATIVE_PATH).read_text(encoding="utf-8")
    config = load_config(tmp_path / CONFIG_RELATIVE_PATH)
    assert list(config.agents) == ["gemini"]
    assert config.settings is not None
    assert config.settings.integrations.invoker.implementation == "cli"
    assert config.settings.integrations.invoker.options == {}
    assert "  gemini:" in config_text
    assert "  # mock:" in config_text
    assert '      # implementation: "mock"' in config_text
    assert "    providers: [gemini]" in workflow_text
    assert "Onboarding connects real provider CLIs" in output
    assert "Selected gemini." in output
    assert "Onboarding will update unchanged generated defaults:" in output
    assert (
        "No backup files will be written because onboarding only updates "
        "unchanged generated defaults."
    ) in output
    assert (
        "It will not start or authenticate gemini, or verify account/model access."
    ) in output
    assert "The generated profiles include configured provider permissions" in output
    assert ".crewplane/config.yml before running" in output
    assert "Provider setup details:" in output
    assert "provider-setup.md" in output
    assert "Generated Crewplane files still match the packaged defaults." not in output
    assert "This activates the generated gemini CLI profile" not in output
    assert "Generated Crewplane files are unchanged." not in output
    assert "Onboarding complete." in output
    assert "Recommended first real run:" in output
    assert "Run without the live dashboard:" in output
    assert "crewplane run" in output
    assert "crewplane run --no-live" in output
    recommended_run_index = output.index("Recommended first real run:")
    no_live_index = output.index("Run without the live dashboard:")
    assert output.index("crewplane run", recommended_run_index) < no_live_index
    assert no_live_index < output.index("crewplane run --no-live", no_live_index)
    assert "has not checked Gemini auth" in output
    assert not any("backup" in path.name.lower() for path in tmp_path.rglob("*"))
    assert which.calls == [*known_provider_names()[:-2], "env", "opencode", "gemini"]


@pytest.mark.parametrize(
    ("stdin", "last_prompt"),
    [("", "Choose [1]:"), ("1\n", "Choose one or more providers")],
)
def test_provider_prompts_eof_leave_files_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdin: str, last_prompt: str
) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)
    paths = [tmp_path / CONFIG_RELATIVE_PATH, tmp_path / WORKFLOW_RELATIVE_PATH]
    before = [path.read_bytes() for path in paths]
    stream = io.StringIO()
    options = make_options(
        tmp_path, stream, [], WhichRecorder({"codex": "/usr/bin/codex"})
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))

    with pytest.raises(EOFError):
        run_onboarding(replace(options, read_input=read_stdin_line))

    assert last_prompt in stream.getvalue()
    assert "Onboarding complete." not in stream.getvalue()
    assert [path.read_bytes() for path in paths] == before


def test_onboarding_selects_opencode_with_native_model_default(tmp_path):
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)
    output, _ = run_onboarding_in_project(
        tmp_path,
        answers=["1", "1", ""],
        which=WhichRecorder({"opencode": "/test/opencode"}),
    )
    config = load_config(tmp_path / CONFIG_RELATIVE_PATH)
    assert list(config.agents) == ["opencode"]
    assert config.agents["opencode"].cli_cmd == ["opencode", "run"]
    assert config.agents["opencode"].default_model is None
    assert "Selected opencode." in output
    assert "Onboarding complete." in output


@pytest.mark.parametrize("select_all", [False, True])
def test_onboarding_enables_multiple_selected_providers(
    tmp_path: Path, select_all: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)
    available = known_provider_names()
    selected = tuple(reversed(available if select_all else available[:2]))
    choices = [str(available.index(provider) + 1) for provider in selected]
    which = WhichRecorder(
        {
            **{
                provider: f"/usr/bin/{provider}"
                for provider in available
                if provider != "deepseek"
            },
            "env": "/usr/bin/env",
            "dsh": "/test/dsh",
        }
    )

    output, _ = run_onboarding_in_project(
        tmp_path, answers=["1", ", ".join([*choices, choices[0]]), ""], which=which
    )

    config = load_config(tmp_path / CONFIG_RELATIVE_PATH)
    assert set(config.agents) == set(selected)
    assert config.settings.integrations.invoker.implementation == "cli"
    assert config.settings.integrations.invoker.options == {}
    workflow_text = (tmp_path / WORKFLOW_RELATIVE_PATH).read_text(encoding="utf-8")
    assert f"    providers: [{', '.join(selected)}]" in workflow_text
    assert f"Selected {', '.join(selected)}." in output
    assert "Onboarding complete." in output
    selected_commands = [
        command
        for provider in selected
        for command in (("env", "dsh") if provider == "deepseek" else (provider,))
    ]
    assert which.calls == [
        *available[:-2],
        "env",
        "dsh",
        "opencode",
        *selected_commands,
    ]


@pytest.mark.parametrize("invalid", ["", " ", ",", "0", "1,0", "1,3", "1,codex"])
def test_provider_selection_requires_at_least_one_valid_provider(
    tmp_path: Path, invalid: str
) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)

    output, _ = run_onboarding_in_project(
        tmp_path,
        answers=["1", invalid, "1,2", ""],
        which=WhichRecorder({"codex": "/usr/bin/codex", "gemini": "/usr/bin/gemini"}),
    )

    assert "Select at least one provider using the listed numbers." in output
    assert "Onboarding complete." in output
    assert set(load_config(tmp_path / CONFIG_RELATIVE_PATH).agents) == {
        "codex",
        "gemini",
    }


def test_declined_file_preparation_leaves_files_unchanged(tmp_path: Path) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)
    default_config = (tmp_path / CONFIG_RELATIVE_PATH).read_text(encoding="utf-8")

    output, _ = run_onboarding_in_project(
        tmp_path,
        answers=["1", "1", "n"],
        which=WhichRecorder({"codex": "/usr/bin/codex"}),
    )

    assert "No files changed." in output
    assert "Manual setup guide:" in output
    assert (tmp_path / CONFIG_RELATIVE_PATH).read_text(
        encoding="utf-8"
    ) == default_config


def test_prewrite_reread_failure_prints_manual_fallback(tmp_path: Path) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)
    default_config = (tmp_path / CONFIG_RELATIVE_PATH).read_text(encoding="utf-8")
    stream = io.StringIO()
    call_count = 0

    def delete_workflow_after_confirmation() -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            (tmp_path / WORKFLOW_RELATIVE_PATH).unlink()
            return ""
        return "1"

    options = OnboardingOptions(
        project_root=tmp_path,
        console=Console(
            file=stream,
            force_terminal=False,
            color_system=None,
            width=120,
        ),
        input_is_terminal=True,
        output_is_terminal=True,
        read_input=delete_workflow_after_confirmation,
        which_fn=WhichRecorder({"codex": "/usr/bin/codex"}),
        write_text=write_text_file,
    )

    run_onboarding(options)

    output = stream.getvalue()
    assert "Generated files could not be re-read before writing" in output
    assert "Manual codex config snippet:" in output
    assert "Validate provider-ready setup" not in output
    assert (tmp_path / CONFIG_RELATIVE_PATH).read_text(
        encoding="utf-8"
    ) == default_config
    assert not (tmp_path / WORKFLOW_RELATIVE_PATH).exists()


def test_prewrite_decode_failure_prints_manual_fallback(tmp_path: Path) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)
    default_config = (tmp_path / CONFIG_RELATIVE_PATH).read_text(encoding="utf-8")
    stream = io.StringIO()
    call_count = 0

    def corrupt_workflow_after_confirmation() -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            (tmp_path / WORKFLOW_RELATIVE_PATH).write_bytes(b"\xff")
            return ""
        return "1"

    options = OnboardingOptions(
        project_root=tmp_path,
        console=Console(
            file=stream,
            force_terminal=False,
            color_system=None,
            width=120,
        ),
        input_is_terminal=True,
        output_is_terminal=True,
        read_input=corrupt_workflow_after_confirmation,
        which_fn=WhichRecorder({"codex": "/usr/bin/codex"}),
        write_text=write_text_file,
    )

    run_onboarding(options)

    output = stream.getvalue()
    assert "Generated files could not be re-read before writing" in output
    assert "Manual codex config snippet:" in output
    assert "Validate provider-ready setup" not in output
    assert (tmp_path / CONFIG_RELATIVE_PATH).read_text(
        encoding="utf-8"
    ) == default_config
    with pytest.raises(UnicodeDecodeError):
        (tmp_path / WORKFLOW_RELATIVE_PATH).read_text(encoding="utf-8")


def test_partial_config_only_update_warns_before_validation(tmp_path: Path) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)

    def fail_workflow(path: Path, content: str) -> None:
        if path.name == "single-agent-review.task.md":
            raise OSError("blocked workflow")
        write_text_file(path, content)

    output, _ = run_onboarding_in_project(
        tmp_path,
        answers=["1", "1", ""],
        which=WhichRecorder({"codex": "/usr/bin/codex"}),
        write_text=fail_workflow,
    )

    assert "Partial onboarding update." in output
    assert "Updated:\n  .crewplane/config.yml" in output
    assert "Not updated:\n  .crewplane/workflows/single-agent-review.task.md" in output
    assert "Validate provider-ready setup" not in output


def test_partial_workflow_only_update_warns_before_validation(tmp_path: Path) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)

    def fail_config(path: Path, content: str) -> None:
        if path.name == "config.yml":
            raise OSError("blocked config")
        write_text_file(path, content)

    output, _ = run_onboarding_in_project(
        tmp_path,
        answers=["1", "1", ""],
        which=WhichRecorder({"codex": "/usr/bin/codex"}),
        write_text=fail_config,
    )

    assert "Partial onboarding update." in output
    assert "Updated:\n  .crewplane/workflows/single-agent-review.task.md" in output
    assert "Not updated:\n  .crewplane/config.yml" in output
    assert "Validate provider-ready setup" not in output


def test_both_write_failures_do_not_claim_partial_update(tmp_path: Path) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)

    def fail_all_writes(path: Path, content: str) -> None:
        del path, content
        raise OSError("blocked")

    output, _ = run_onboarding_in_project(
        tmp_path,
        answers=["1", "1", ""],
        which=WhichRecorder({"codex": "/usr/bin/codex"}),
        write_text=fail_all_writes,
    )

    assert "No onboarding files were updated." in output
    assert "Partial onboarding update." not in output
    assert "Validate provider-ready setup" not in output


def test_validation_failure_keeps_written_files_without_rollback(
    tmp_path: Path,
) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)
    stream = io.StringIO()
    which = DetectionOnlyCodex()
    options = make_options(tmp_path, stream, ["1", "1", ""], which)

    with pytest.raises(typer.Exit):
        run_onboarding(options)

    output = stream.getvalue()
    assert "Saved settings for codex did not pass validation." in output
    assert "Files were not rolled back." in output
    assert load_config(tmp_path / CONFIG_RELATIVE_PATH).settings is not None
    assert "    providers: [codex]" in (tmp_path / WORKFLOW_RELATIVE_PATH).read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    "failed_paths",
    [
        (),
        (CONFIG_RELATIVE_PATH,),
        (WORKFLOW_RELATIVE_PATH,),
        (CONFIG_RELATIVE_PATH, WORKFLOW_RELATIVE_PATH),
    ],
)
def test_file_writes_and_reports_remain_in_config_workflow_order(
    tmp_path: Path, failed_paths: tuple[Path, ...]
) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)
    stream = io.StringIO()
    calls: list[Path] = []

    def write(path: Path, content: str) -> None:
        if calls:
            report = "Could not update" if calls[-1] in failed_paths else "Updated"
            assert f"{report} {calls[-1]}" in stream.getvalue()
        calls.append(path.relative_to(tmp_path))
        if calls[-1] in failed_paths:
            raise OSError("write blocked")
        write_text_file(path, content)

    result = run_onboarding(
        make_options(
            tmp_path,
            stream,
            ["1", "1", ""],
            WhichRecorder({"codex": "/usr/bin/codex"}),
            write_text=write,
        )
    )

    assert result is None
    assert calls == [CONFIG_RELATIVE_PATH, WORKFLOW_RELATIVE_PATH]
    for path in calls:
        expected = (
            rendered_default_config()
            if path == CONFIG_RELATIVE_PATH
            else rendered_default_workflow()
        )
        assert ((tmp_path / path).read_text(encoding="utf-8") == expected) == (
            path in failed_paths
        )
    assert ("Onboarding complete." in stream.getvalue()) == (not failed_paths)


@pytest.mark.parametrize("failed_path", [CONFIG_RELATIVE_PATH, WORKFLOW_RELATIVE_PATH])
def test_non_os_write_errors_propagate_and_stop_remaining_writes(
    tmp_path: Path, failed_path: Path
) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)
    stream = io.StringIO()
    calls: list[Path] = []
    error = ValueError("writer rejected content")

    def write(path: Path, content: str) -> None:
        calls.append(path.relative_to(tmp_path))
        if calls[-1] == failed_path:
            raise error
        write_text_file(path, content)

    with pytest.raises(ValueError) as caught:
        run_onboarding(
            make_options(
                tmp_path,
                stream,
                ["1", "1", ""],
                WhichRecorder({"codex": "/usr/bin/codex"}),
                write_text=write,
            )
        )

    assert caught.value is error
    assert calls == (
        [CONFIG_RELATIVE_PATH]
        if failed_path == CONFIG_RELATIVE_PATH
        else [CONFIG_RELATIVE_PATH, WORKFLOW_RELATIVE_PATH]
    )
    assert "Validate provider-ready setup" not in stream.getvalue()
    assert "Could not update" not in stream.getvalue()
    assert (
        load_config(
            tmp_path / CONFIG_RELATIVE_PATH
        ).settings.integrations.invoker.implementation
        == "cli"
    ) == (failed_path == WORKFLOW_RELATIVE_PATH)
    assert (
        tmp_path / WORKFLOW_RELATIVE_PATH
    ).read_text() == rendered_default_workflow()


@pytest.mark.parametrize(
    ("other_code", "other_severity", "failure_type"),
    [
        (PreflightDiagnosticCode.PROVIDER_CLI, "error", ProviderReadinessFailure),
        (PreflightDiagnosticCode.DAG, "error", typer.Exit),
        (PreflightDiagnosticCode.DAG, "warning", ProviderReadinessFailure),
    ],
)
def test_validation_recovers_only_when_all_errors_are_provider_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    other_code,
    other_severity,
    failure_type,
) -> None:
    initialize_default_project(tmp_path)
    stream = io.StringIO()
    runner = OnboardingRunner(make_options(tmp_path, stream, [], WhichRecorder()))
    preview = PreflightCompilationPreview(
        diagnostics=[
            PreflightDiagnostic(
                code=PreflightDiagnosticCode.PROVIDER_CLI,
                message="provider unavailable",
                phase=PreflightDiagnosticPhase.PROVIDER,
            ),
            PreflightDiagnostic(
                code=other_code,
                severity=other_severity,
                message="other diagnostic",
                phase=PreflightDiagnosticPhase.VALIDATION,
            ),
        ]
    )
    compile_preview = Mock(return_value=preview)
    monkeypatch.setattr(workflow_runner, "compile_workflow_preview", compile_preview)

    with pytest.raises(typer.Exit) as caught:
        runner.validate_provider_ready_setup("codex")

    assert type(caught.value) is failure_type
    assert caught.value.exit_code == 1
    assert "Saved settings for codex did not pass validation." in stream.getvalue()
    assert "Preflight plan compiled" not in stream.getvalue()
    arguments = compile_preview.call_args.kwargs
    assert arguments["no_live"] is True
    assert arguments["fingerprint_key_policy"] == "read_only"
    assert arguments["check_cli_availability"] is True
    assert arguments["workspace_real_execution"] is False
    assert arguments["project_root"] == tmp_path
    assert arguments["state_dir"] == tmp_path / ".crewplane"


@pytest.mark.parametrize(
    "error", [ValueError("invalid preview"), typer.Exit(code=7), KeyboardInterrupt()]
)
def test_validation_preserves_exception_conversion_and_interruptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    initialize_default_project(tmp_path)
    stream = io.StringIO()
    runner = OnboardingRunner(make_options(tmp_path, stream, [], WhichRecorder()))
    monkeypatch.setattr(
        workflow_runner, "compile_workflow_preview", Mock(side_effect=error)
    )
    expected_type = typer.Exit if isinstance(error, ValueError) else type(error)

    with pytest.raises(expected_type) as caught:
        runner.validate_provider_ready_setup("codex")

    if isinstance(error, ValueError):
        assert caught.value.__cause__ is error
        assert caught.value.exit_code == 1
        assert "Validation failed: invalid preview" in stream.getvalue()
    else:
        assert caught.value is error
    assert (
        "Saved settings for codex did not pass validation." in stream.getvalue()
    ) == (not isinstance(error, KeyboardInterrupt))


def run_onboarding_in_project(
    root: Path,
    answers: list[str] | None = None,
    which: WhichRecorder | None = None,
    interactive: bool = True,
    write_text=write_text_file,
) -> tuple[str, WhichRecorder]:
    stream = io.StringIO()
    selected_which = which or WhichRecorder()
    options = make_options(
        root, stream, answers or [], selected_which, interactive, write_text
    )
    run_onboarding(options)
    return stream.getvalue().replace("\\", "/"), selected_which


def make_options(
    root: Path,
    stream: io.StringIO,
    answers: list[str],
    which,
    interactive: bool = True,
    write_text=write_text_file,
) -> OnboardingOptions:
    queue = deque(answers)
    return OnboardingOptions(
        project_root=root,
        console=Console(
            file=stream,
            force_terminal=False,
            color_system=None,
            width=120,
        ),
        input_is_terminal=interactive,
        output_is_terminal=interactive,
        read_input=queue.popleft,
        which_fn=which,
        write_text=write_text,
    )


def initialize_default_project(root: Path) -> None:
    console = Console(file=io.StringIO(), force_terminal=False, color_system=None)
    initialize_project_templates(console, root)


def write_successful_mock_history(root: Path) -> None:
    console = Console(file=io.StringIO(), force_terminal=False, color_system=None)
    config = load_config(root / CONFIG_RELATIVE_PATH)
    source = load_workflow_source_for_preflight(
        root / WORKFLOW_RELATIVE_PATH,
        project_root=root,
    )
    preview = compile_workflow_preview(
        config=config,
        source=source,
        console=console,
        no_live=True,
        fingerprint_key_policy="read_only",
        project_root=root,
        state_dir=root / ".crewplane",
        check_cli_availability=False,
    )
    raise_for_preflight_preview_errors(preview, console)
    assert preview.workflow_name is not None
    assert preview.workflow_signature is not None
    run_key_name = build_run_key_name(preview.workflow_name, "mock-run")
    manifest = make_run_manifest(
        run_id="mock-run",
        run_key_name=run_key_name,
        status="succeeded",
        workflow_identity=".crewplane/workflows/single-agent-review.task.md",
        workflow_name=preview.workflow_name,
        workflow_signature=preview.workflow_signature,
    )
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "invoker": {
            "implementation": "mock",
            "resolved_identity": resolve_implementation_path("invoker", "mock"),
            "options": {},
        },
    }
    write_run_manifest(
        root / ".crewplane",
        manifest.model_copy(update={"runtime_config_snapshot": snapshot}),
    )


@pytest.mark.parametrize("provider", ["pi", "deepseek"])
def test_generated_text_profile_validates_and_dry_runs_without_launch(
    tmp_path, monkeypatch, provider
) -> None:
    initialize_default_project(tmp_path)
    (tmp_path / CONFIG_RELATIVE_PATH).write_text(
        render_provider_ready_config(rendered_default_config(), (provider,)),
        encoding="utf-8",
        newline="\n",
    )
    (tmp_path / WORKFLOW_RELATIVE_PATH).write_text(
        render_provider_ready_workflow(rendered_default_workflow(), (provider,)),
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.chdir(tmp_path)

    def find_provider(executable, path=None):  # noqa: ARG001
        assert executable in {"pi", "dsh", "env"}
        return "/usr/bin/env" if executable == "env" else f"/test/{executable}"

    def unexpected_invoker(*args, **kwargs):
        del args, kwargs
        raise AssertionError("preflight must not construct an invoker")

    monkeypatch.setattr("crewplane.adapters.invokers.cli.shutil.which", find_provider)
    monkeypatch.setattr(CliInvokerAdapter, "create_invoker", unexpected_invoker)
    for arguments in (["validate"], ["run", "--dry-run", "--no-live"]):
        result = CliRunner().invoke(cli.app, arguments)
        assert result.exit_code == 0, result.output
        if provider == "deepseek":
            assert "argv" in result.output
    assert not list((tmp_path / ".crewplane/execution-stages").glob("*"))


@pytest.mark.parametrize(
    "arguments", [["validate"], ["run", "--dry-run"], ["run", "--no-live"]]
)
def test_pi_conflicts_fail_all_cli_preflight_routes(
    tmp_path, monkeypatch, arguments
) -> None:
    initialize_default_project(tmp_path)
    (tmp_path / CONFIG_RELATIVE_PATH).write_text(
        render_provider_ready_config(rendered_default_config(), ("pi",)).replace(
            'cli_cmd: ["pi"]', 'cli_cmd: ["pi", "--mode", "rpc"]'
        )
    )
    (tmp_path / WORKFLOW_RELATIVE_PATH).write_text(
        render_provider_ready_workflow(rendered_default_workflow(), ("pi",))
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code != 0
    assert "Pi requires --mode text" in result.output
    assert not list((tmp_path / ".crewplane/execution-results").glob("*.md"))

from __future__ import annotations

import io
from collections import deque
from pathlib import Path

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

import crewplane.cli.app as cli
from crewplane.artifacts.naming import build_run_key_name
from crewplane.cli.onboarding import (
    CONFIG_RELATIVE_PATH,
    ONBOARDING_COMMAND_HELP,
    WORKFLOW_RELATIVE_PATH,
    OnboardingOptions,
    run_onboarding,
    write_text_file,
)
from crewplane.cli.onboarding.history import MOCK_INVOKER_RESOLVED_IDENTITY
from crewplane.cli.onboarding.rendering import (
    render_provider_ready_config,
    render_provider_ready_workflow,
    rendered_default_config,
    rendered_default_workflow,
)
from crewplane.cli.onboarding.runner import OnboardingRunner
from crewplane.cli.project_init import initialize_project_templates
from crewplane.cli.run.preflight import (
    compile_workflow_preview,
    raise_for_preflight_preview_errors,
)
from crewplane.core.config import load_config
from crewplane.core.preflight import load_workflow_source_for_preflight
from crewplane.core.provider_names import known_provider_names
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import make_run_manifest, write_run_manifest


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
    config_text = render_provider_ready_config(rendered_default_config(), "codex")
    workflow_text = render_provider_ready_workflow(rendered_default_workflow(), "codex")
    config_path.write_text(config_text, encoding="utf-8")
    workflow_path.write_text(workflow_text, encoding="utf-8")

    output, which = run_onboarding_in_project(tmp_path)

    assert "Crewplane onboarding" in output
    assert "Onboarding connects one real provider CLI" not in output
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
    config_text = render_provider_ready_config(rendered_default_config(), "codex")
    workflow_text = workflow_path.read_text(encoding="utf-8")
    config_path.write_text(config_text, encoding="utf-8")

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
    assert "Onboarding connects one real provider CLI" not in output
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
    original_generated_files_exist = OnboardingRunner.generated_files_exist

    def generated_files_exist_then_remove_workflow(
        runner: OnboardingRunner,
    ) -> bool:
        generated_files_exist = original_generated_files_exist(runner)
        if generated_files_exist:
            (tmp_path / WORKFLOW_RELATIVE_PATH).unlink()
        return generated_files_exist

    monkeypatch.setattr(
        OnboardingRunner,
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


def test_missing_mock_evidence_can_continue_and_skip_provider(tmp_path: Path) -> None:
    initialize_default_project(tmp_path)

    output, which = run_onboarding_in_project(
        tmp_path,
        answers=["1", "0"],
        which=WhichRecorder({"codex": "/usr/bin/codex"}),
    )

    assert "Provider detection" in output
    assert "Onboarding skipped provider setup." in output
    assert which.calls == list(known_provider_names())


def test_no_provider_found_stops_with_setup_guidance(tmp_path: Path) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)

    output, which = run_onboarding_in_project(tmp_path, which=WhichRecorder())

    assert "No known provider CLI names were found on PATH." in output
    assert "provider-setup.md" in output
    assert sorted(which.calls) == sorted(known_provider_names())


def test_successful_onboarding_selects_provider_writes_and_validates(
    tmp_path: Path,
) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)
    which = WhichRecorder({"codex": "/usr/bin/codex", "gemini": "/usr/bin/gemini"})

    output, _ = run_onboarding_in_project(tmp_path, answers=["2", ""], which=which)

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
    assert "Onboarding connects one real provider CLI" in output
    assert "Selected gemini." in output
    assert "Onboarding will update unchanged generated defaults:" in output
    assert (
        "No backup files will be written because onboarding only updates "
        "unchanged generated defaults."
    ) in output
    assert (
        "It will not start gemini, authenticate it, or verify account/model access."
    ) in output
    assert (
        "The generated gemini profile includes configured provider permissions"
        in output
    )
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
    assert which.calls == [*known_provider_names(), "gemini"]


def test_provider_selection_skip_leaves_files_unchanged(tmp_path: Path) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)
    default_config = (tmp_path / CONFIG_RELATIVE_PATH).read_text(encoding="utf-8")

    output, _ = run_onboarding_in_project(
        tmp_path,
        answers=["0"],
        which=WhichRecorder({"codex": "/usr/bin/codex"}),
    )

    assert "Onboarding skipped provider setup." in output
    assert (tmp_path / CONFIG_RELATIVE_PATH).read_text(
        encoding="utf-8"
    ) == default_config


def test_declined_file_preparation_leaves_files_unchanged(tmp_path: Path) -> None:
    initialize_default_project(tmp_path)
    write_successful_mock_history(tmp_path)
    default_config = (tmp_path / CONFIG_RELATIVE_PATH).read_text(encoding="utf-8")

    output, _ = run_onboarding_in_project(
        tmp_path,
        answers=["1", "n"],
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
        if call_count == 2:
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
        if call_count == 2:
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
        answers=["1", ""],
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
        answers=["1", ""],
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
        answers=["1", ""],
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
    options = make_options(tmp_path, stream, ["1", ""], which)

    with pytest.raises(typer.Exit):
        run_onboarding(options)

    output = stream.getvalue()
    assert "Provider-ready validation failed for codex." in output
    assert "Files were not rolled back." in output
    assert load_config(tmp_path / CONFIG_RELATIVE_PATH).settings is not None
    assert "    providers: [codex]" in (tmp_path / WORKFLOW_RELATIVE_PATH).read_text(
        encoding="utf-8"
    )


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
    return stream.getvalue(), selected_which


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
        read_input=lambda: queue.popleft() if queue else "",
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
            "resolved_identity": MOCK_INVOKER_RESOLVED_IDENTITY,
            "options": {},
        },
    }
    write_run_manifest(
        root / ".crewplane",
        manifest.model_copy(update={"runtime_config_snapshot": snapshot}),
    )

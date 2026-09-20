import io
import json

import pytest
from rich.console import Console
from typer.testing import CliRunner

import crewplane.cli.app as cli
from crewplane.adapters.invokers.cli import CliInvokerAdapter
from crewplane.cli.onboarding.rendering import (
    render_provider_ready_config,
    render_provider_ready_workflow,
    rendered_default_config,
    rendered_default_workflow,
)
from crewplane.cli.project_init import initialize_project_templates
from crewplane.core.config import load_config


@pytest.fixture
def project(tmp_path, monkeypatch):
    state = initialize_project_templates(Console(file=io.StringIO()), tmp_path)
    (state / "config.yml").write_text(
        render_provider_ready_config(rendered_default_config(), ("opencode",))
    )
    (state / "workflows/single-agent-review.task.md").write_text(
        render_provider_ready_workflow(rendered_default_workflow(), ("opencode",))
    )
    monkeypatch.chdir(tmp_path)

    def unexpected_invoker(*args, **kwargs):
        del args, kwargs
        raise AssertionError("preflight must not construct an invoker")

    monkeypatch.setattr(CliInvokerAdapter, "create_invoker", unexpected_invoker)
    return state


@pytest.mark.parametrize("alias", ["opencode", "project-worker"])
def test_generated_profile_validates_and_dry_runs_without_launch(
    project, monkeypatch, alias
):
    config_path = project / "config.yml"
    config_path.write_text(
        config_path.read_text().replace("  opencode:", f"  {alias}:")
    )
    workflow_path = project / "workflows/single-agent-review.task.md"
    workflow_path.write_text(
        workflow_path.read_text().replace(
            "providers: [opencode]", f"providers: [{alias}]"
        )
    )
    found = {"opencode": "/test/opencode"}
    monkeypatch.setattr("crewplane.adapters.invokers.cli.shutil.which", found.get)
    before = {path: path.read_bytes() for path in project.rglob("*") if path.is_file()}
    config = load_config(config_path)
    assert config.settings.integrations.invoker.implementation == "cli"
    assert config.agents[alias].default_model is None
    for arguments in (["validate"], ["run", "--dry-run", "--no-live"]):
        result = CliRunner().invoke(cli.app, arguments)
        assert result.exit_code == 0, result.output
    assert {
        path: path.read_bytes() for path in project.rglob("*") if path.is_file()
    } == before
    assert not list((project / "execution-stages").glob("*"))
    assert not list((project / "execution-results").glob("*"))


@pytest.mark.parametrize(
    "arguments", [["validate"], ["run", "--dry-run"], ["run", "--no-live"]]
)
@pytest.mark.parametrize(
    "native_args",
    [
        "--format=json",
        "--continue",
        "--dir=elsewhere",
        "--title",
        "--model=raw",
        "--agent=plan, --agent=plan",
        "--variant=low, --variant=high",
        "--title=first, --title=second",
        "--log-level=INFO, --log-level=ERROR",
        "positional-prompt",
    ],
)
def test_d2_conflicts_fail_before_launch(project, arguments, native_args):
    config_path = project / "config.yml"
    config_path.write_text(
        config_path.read_text().replace(
            "cli_cmd: [opencode, run]",
            f"cli_cmd: [opencode, run, {native_args}]\n    default_model: provider/model",
        )
    )
    result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code != 0
    assert "OpenCode" in result.output
    assert not list((project / "execution-results").glob("**/*.md"))
    assert not list((project / "execution-stages").glob("*/manifest.json"))
    if arguments != ["run", "--no-live"]:
        assert not list((project / "execution-stages").glob("*"))


@pytest.mark.parametrize(
    "arguments", [["validate"], ["run", "--dry-run"], ["run", "--no-live"]]
)
@pytest.mark.parametrize("source", ["workflow", "default"])
@pytest.mark.parametrize("model", ["--continue", "--auto", "--session=ses_private"])
def test_invalid_resolved_models_fail_preflight(
    project, monkeypatch, arguments, source, model
):
    found = {"opencode": "/test/opencode"}
    monkeypatch.setattr("crewplane.adapters.invokers.cli.shutil.which", found.get)
    if source == "default":
        path = project / "config.yml"
        path.write_text(
            path.read_text().replace(
                "cli_cmd: [opencode, run]",
                f"cli_cmd: [opencode, run]\n    default_model: {json.dumps(model)}",
            )
        )
    else:
        path = project / "workflows/single-agent-review.task.md"
        path.write_text(
            path.read_text().replace(
                "providers: [opencode]",
                f"providers: [{{provider: opencode, model: {json.dumps(model)}}}]",
            )
        )
    result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code != 0
    assert "OpenCode option '--model' requires a nonblank value." in result.output
    assert "ses_private" not in result.output
    assert not list((project / "execution-results").glob("**/*.md"))
    assert not list((project / "execution-stages").glob("*/manifest.json"))
    if arguments != ["run", "--no-live"]:
        assert not list((project / "execution-stages").glob("*"))


def test_missing_executable_fails_executing_run_preflight(project, monkeypatch):
    monkeypatch.setenv("PATH", "")
    result = CliRunner().invoke(cli.app, ["run", "--no-live"])
    assert result.exit_code != 0
    assert "opencode" in result.output and "not found in PATH" in result.output
    assert not list((project / "execution-results").glob("**/*.md"))


def test_missing_executable_is_checked_by_validate_and_skipped_by_dry_run(
    project, monkeypatch
):
    monkeypatch.setenv("PATH", "")
    for arguments in (["validate"], ["run", "--dry-run"]):
        result = CliRunner().invoke(cli.app, arguments)
        assert result.exit_code == (1 if arguments == ["validate"] else 0), (
            result.output
        )
    assert not list((project / "execution-stages").glob("*"))

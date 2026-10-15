import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from crewplane.cli.app import app
from crewplane.cli.onboarding.rendering_config_agents import uncomment_provider_agent
from crewplane.cli.onboarding.rendering_config_invoker import activate_cli_invoker
from crewplane.cli.onboarding.rendering_yaml_blocks import uncomment_yaml_comment_block
from tests.helpers.cli_provider import command_receipts


def test_generated_ollama_config_runs_bundled_text_workflow(
    ollama: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path), prepend=os.pathsep)
    runner = CliRunner()
    initialized = runner.invoke(app, ["init"])
    assert initialized.exit_code == 0, initialized.output

    state_dir = tmp_path / ".crewplane"
    config_path = state_dir / "config.yml"
    config = uncomment_provider_agent(config_path.read_text(encoding="utf-8"), "local")
    config = activate_cli_invoker(config)
    start = config.index("      # options:\n")
    end = config.index("\n      #\n", start)
    options = uncomment_yaml_comment_block(config[start:end], 6)
    config = config.replace("      options: {}", options, 1)
    config_path.write_text(
        config.replace("<installed-local-text-model>", "provisioned-text-model"),
        encoding="utf-8",
    )

    examples = state_dir / "workflows/example-templates"
    workflow = examples / "text-generation-example.task.md"
    validated = runner.invoke(app, ["validate", str(workflow)])
    assert validated.exit_code == 0, validated.output
    assert not (state_dir / "execution-stages").exists()

    executed = runner.invoke(app, ["run", "--no-live", "--tasks", str(workflow)])
    assert executed.exit_code == 0, executed.output
    invocations = [
        receipt
        for receipt in command_receipts(ollama.receipts, "run")
        if receipt["stdin"] != "Reply with OK only."
    ]
    assert len(invocations) == 2
    brief = (examples / "sample-inputs/feature-brief.md").read_text(encoding="utf-8")
    assert brief.strip() in invocations[0]["stdin"]
    results = next((state_dir / "execution-results").iterdir())
    upstream = (results / "upstream-result.md").read_text(encoding="utf-8")
    assert upstream.strip() in invocations[1]["stdin"]
    assert (results / "refine-result.md").read_text(encoding="utf-8").strip()

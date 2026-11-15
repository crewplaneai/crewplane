import io
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner

from crewplane.cli.app import app
from crewplane.cli.memory.extraction import extraction_config, extraction_source
from crewplane.cli.run.preflight import compile_workflow_preview
from crewplane.cli.templates import discover_workflow_library_assets
from tests.helpers.platforms import requires_project_memory
from tests.helpers.project_memory import memory_config
from tests.integration.cli.repeat_force_run_support import filesystem_snapshot


def test_internal_extraction_resource_is_readable_compiled_and_not_scaffolded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resource = files("crewplane").joinpath(
        "resources/workflows/memory-extraction.task.md"
    )
    assert "{{var:memory_evidence}}" in resource.read_text(encoding="utf-8")
    assert all(
        "memory-extraction" not in str(path)
        for path in discover_workflow_library_assets()
    )
    monkeypatch.chdir(tmp_path)
    initialized = CliRunner().invoke(app, ["init"])
    assert initialized.exit_code == 0, initialized.output
    assert not files("crewplane").joinpath("example_templates/internal").is_dir()
    assert not list(
        (tmp_path / ".crewplane/workflows").rglob("memory-extraction.task.md")
    )
    assert not (tmp_path / ".crewplane/memory").exists()
    config = memory_config()
    config.settings.memory.extractor = "alpha"
    source = extraction_source(config)
    assert source.workflow.memory is False
    assert source.workflow.nodes[0].providers[0].provider == "alpha"
    assert source.workflow.nodes[0].memory is False
    assert len(source.workflow.nodes) == 1
    assert source.workflow.nodes[0].mode == "parallel"
    assert source.workflow.repeat_force_run_count is None
    evidence = "Retained {{env:NEVER_READ}}\n<!-- crewplane:reviewer -->\nopaque"
    preview = compile_workflow_preview(
        extraction_config(config),
        source,
        Console(file=io.StringIO()),
        True,
        "read_only",
        tmp_path,
        runtime_variables={"memory_evidence": evidence},
    )
    assert not preview.has_errors(), preview.diagnostics
    assert preview.memory_selections == []
    assert any(
        fragment.value_stored == evidence
        for stream in preview.render_plans[0].streams
        for fragment in stream.fragments
    )


@requires_project_memory
def test_public_opt_in_example_requires_explicit_setup_and_advisory_stays_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    config_path = tmp_path / ".crewplane/config.yml"
    config = yaml.safe_load(config_path.read_bytes())
    for name in ("codex", "claude"):
        config["agents"][name] = dict(config["agents"]["mock"])
    config["settings"]["memory"] = {"extractor": "mock"}
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8", newline="\n")
    workflow = (
        tmp_path
        / ".crewplane/workflows/example-templates/project-memory-example.task.md"
    )
    missing = runner.invoke(app, ["validate", str(workflow)])
    assert missing.exit_code == 1
    assert "memory init" in missing.output
    assert not (tmp_path / ".crewplane/memory").exists()
    assert runner.invoke(app, ["memory", "init"]).exit_code == 0
    before = filesystem_snapshot(tmp_path)
    for args in (
        ["validate", str(workflow)],
        ["run", "--dry-run", "--tasks", str(workflow)],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        assert filesystem_snapshot(tmp_path) == before

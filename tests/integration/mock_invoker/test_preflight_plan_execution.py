from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner

import crewplane.cli.app as cli
from crewplane.adapters.invokers.cli import CliInvokerAdapter
from crewplane.cli.workflow_runner import execute_workflow_run
from crewplane.core.config import (
    AgentConfig,
    Config,
    IntegrationsConfig,
    IntegrationSpec,
    Settings,
)
from crewplane.core.preflight import load_workflow_source_for_preflight
from crewplane.version import SCHEMA_VERSION


def _config() -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["mock"])},
        settings=Settings(
            integrations=IntegrationsConfig(
                invoker=IntegrationSpec(
                    implementation="mock",
                    options={"output_mode": "echo", "observation_delay_seconds": 0},
                ),
                artifacts=IntegrationSpec(
                    implementation="filesystem",
                    options={"log_cli_output": True},
                ),
                ui=IntegrationSpec(implementation="none", options={}),
            )
        ),
    )


def _workflow_text() -> str:
    return "\n".join(
        [
            "---",
            f'schema_version: "{SCHEMA_VERSION}"',
            "name: Preflight Integration",
            "nodes:",
            "  - id: build",
            "    mode: sequential",
            "    providers: [alpha]",
            "  - id: review",
            "    mode: sequential",
            "    needs: [build]",
            "    providers: [alpha]",
            "---",
            "",
            "## build",
            "",
            "File: {{file:context.md}}",
            "Env: {{env:PREFLIGHT_PLAN_EXECUTION_TOKEN}}",
            "Var: {{var:project_name}}",
            "",
            "## review",
            "",
            "Review {{build.output}}",
        ]
    )


def _imported_workflow_text() -> str:
    return "\n".join(
        [
            "---",
            f'schema_version: "{SCHEMA_VERSION}"',
            "name: Imported Child",
            "nodes:",
            "  - id: build",
            "    mode: sequential",
            "    providers: [alpha]",
            "---",
            "",
            "## build",
            "",
            "File: {{file:.crewplane/workflows/modules/context.md}}",
            "Env: {{env:PREFLIGHT_IMPORTED_TOKEN}}",
            "Var: {{var:project_name}}",
        ]
    )


def _root_import_workflow_text() -> str:
    return "\n".join(
        [
            "---",
            f'schema_version: "{SCHEMA_VERSION}"',
            "name: Imported Root",
            "imports:",
            "  - path: modules/child.task.md",
            "    as: child",
            "nodes:",
            "  - id: summarize",
            "    mode: sequential",
            "    needs: [child.build]",
            "    providers: [alpha]",
            "---",
            "",
            "## summarize",
            "",
            "Summarize {{child.build.output}}",
        ]
    )


def _run_dirs(root: Path) -> list[Path]:
    stages_root = root / ".crewplane" / "execution-stages"
    return sorted(path for path in stages_root.iterdir() if path.is_dir())


def _write_reasoning_project(root: Path, config: Config) -> tuple[Path, Path]:
    config_path = root / ".crewplane/config.yml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8", newline="\n"
    )
    workflow_path = root / "reasoning.task.md"
    workflow_path.write_text(
        """---
name: Mock reasoning
nodes:
  - id: build
    mode: sequential
    providers:
      - {provider: alpha, role: executor, reasoning: xhigh}
      - {provider: alpha, role: reviewer, reasoning: max}
---
## build
Build with reasoning.
""",
        encoding="utf-8",
        newline="\n",
    )
    return workflow_path, config_path


@pytest.mark.parametrize(
    ("provider_kind", "extra_args"),
    [
        ("generic", []),
        ("codex", ["-c", 'model_reasoning_effort="low"']),
        ("claude", ["--effort", "low"]),
    ],
)
def test_mock_reasoning_validate_dry_run_and_execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_kind: str,
    extra_args: list[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")
    cli_invoker = Mock(side_effect=AssertionError("mock constructed a CLI invoker"))
    monkeypatch.setattr(CliInvokerAdapter, "create_invoker", cli_invoker)
    config = _config()
    config.agents["alpha"] = AgentConfig.model_validate(
        {
            "cli_cmd": ["definitely-not-installed-cli"],
            "provider_kind": provider_kind,
            "extra_args": extra_args,
        }
    )
    workflow, config_path = _write_reasoning_project(tmp_path, config)
    runner = CliRunner()
    arguments = ["--tasks", str(workflow), "--config", str(config_path)]

    valid = runner.invoke(
        cli.app, ["validate", str(workflow), "--config", str(config_path)]
    )
    assert valid.exit_code == 0, (valid.output, valid.exception)
    dry = runner.invoke(cli.app, ["run", *arguments, "--dry-run"])
    assert dry.exit_code == 0, (dry.output, dry.exception)
    assert "xhigh" in dry.output
    assert "max" in dry.output
    assert not (tmp_path / ".crewplane/execution-stages").exists()
    result = runner.invoke(cli.app, ["run", *arguments, "--no-live"])
    assert result.exit_code == 0, (result.output, result.exception)

    (run_dir,) = _run_dirs(tmp_path)
    plan = json.loads((run_dir / "preflight/execution-plan.json").read_bytes())
    assert [
        (provider["role"], provider["requested_reasoning"])
        for provider in plan["nodes"][0]["provider_records"]
    ] == [("executor", "xhigh"), ("reviewer", "max")]
    manifest = json.loads((run_dir / "manifests/run.json").read_bytes())
    assert manifest["status"] == "succeeded"
    assert manifest["workflow_signature"] == plan["workflow_signature"]
    result_path = (
        tmp_path / ".crewplane/execution-results" / run_dir.name / "build-result.md"
    )
    assert "Build with reasoning." in result_path.read_text(encoding="utf-8")
    events = [
        json.loads(line)
        for line in (run_dir / "logs/events.ndjson")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert {
        (event["role"], event["requested_reasoning"])
        for event in events
        if event["event_type"] == "invocation_started"
    } == {("executor", "xhigh"), ("reviewer", "max")}
    cli_invoker.assert_not_called()


@pytest.mark.parametrize("failure", ["fail_when", "missing_fixture"])
def test_mock_reasoning_preserves_invocation_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.chdir(tmp_path)
    config = _config()
    config.agents["alpha"].cli_cmd = ["definitely-not-installed-cli"]
    options = config.settings.integrations.invoker.options
    if failure == "fail_when":
        options["fail_when"] = [{"node_id": "build"}]
    else:
        options.update(
            output_mode="file",
            output_dir=str(tmp_path / "missing-fixtures"),
            strict_file_mode=True,
        )
    workflow, config_path = _write_reasoning_project(tmp_path, config)

    result = CliRunner().invoke(
        cli.app,
        ["run", "--tasks", str(workflow), "--config", str(config_path), "--no-live"],
    )

    assert result.exit_code == 1, (result.output, result.exception)
    (run_dir,) = _run_dirs(tmp_path)
    manifest = json.loads((run_dir / "manifests/run.json").read_bytes())
    assert manifest["status"] == "failed"
    plan = json.loads((run_dir / "preflight/execution-plan.json").read_bytes())
    assert plan["nodes"][0]["provider_records"][0]["requested_reasoning"] == "xhigh"


def test_mock_invoker_executes_preflight_plan_with_static_and_node_refs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workflow_path = tmp_path / ".crewplane" / "workflows" / "workflow.task.md"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text(_workflow_text(), encoding="utf-8")
    (tmp_path / "context.md").write_text("module context", encoding="utf-8")
    monkeypatch.setenv("PREFLIGHT_PLAN_EXECUTION_TOKEN", "secret-from-env")
    monkeypatch.chdir(tmp_path)
    source = load_workflow_source_for_preflight(workflow_path, project_root=tmp_path)

    asyncio.run(
        execute_workflow_run(
            config=_config(),
            source=source,
            force=False,
            no_live=True,
            console=Console(file=io.StringIO(), force_terminal=False),
        )
    )

    run_dir = _run_dirs(tmp_path)[0]
    preflight_dir = run_dir / "preflight"
    plan = json.loads((preflight_dir / "execution-plan.json").read_text("utf-8"))
    token_catalog = json.loads(
        (preflight_dir / "token-catalog.json").read_text("utf-8")
    )
    result_dir = tmp_path / ".crewplane" / "execution-results" / run_dir.name
    review_result = (result_dir / "review-result.md").read_text(encoding="utf-8")

    assert plan["plan_schema_version"] == SCHEMA_VERSION
    assert "schema_version" not in plan
    assert "secret-from-env" not in json.dumps(plan)
    assert any(entry["token_kind"] == "file" for entry in token_catalog)
    assert any(entry["token_kind"] == "env" for entry in token_catalog)
    assert any(entry["token_kind"] == "var" for entry in token_catalog)
    assert any(entry["token_kind"] == "node" for entry in token_catalog)
    assert "module context" in review_result
    assert os.environ["PREFLIGHT_PLAN_EXECUTION_TOKEN"] == "secret-from-env"


def test_mock_invoker_executes_imported_preflight_plan_with_project_root_file_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workflow_root = tmp_path / ".crewplane" / "workflows"
    workflow_path = workflow_root / "root.task.md"
    imported_workflow_path = workflow_root / "modules" / "child.task.md"
    imported_context_path = workflow_root / "modules" / "context.md"
    imported_workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text(_root_import_workflow_text(), encoding="utf-8")
    imported_workflow_path.write_text(_imported_workflow_text(), encoding="utf-8")
    imported_context_path.write_text("imported module context", encoding="utf-8")
    monkeypatch.setenv("PREFLIGHT_IMPORTED_TOKEN", "secret-from-import")
    monkeypatch.chdir(tmp_path)
    source = load_workflow_source_for_preflight(workflow_path, project_root=tmp_path)

    asyncio.run(
        execute_workflow_run(
            config=_config(),
            source=source,
            force=False,
            no_live=True,
            console=Console(file=io.StringIO(), force_terminal=False),
        )
    )

    run_dir = _run_dirs(tmp_path)[0]
    preflight_dir = run_dir / "preflight"
    plan = json.loads((preflight_dir / "execution-plan.json").read_text("utf-8"))
    result_dir = tmp_path / ".crewplane" / "execution-results" / run_dir.name
    summarize_result = (result_dir / "summarize-result.md").read_text(encoding="utf-8")

    static_resources = plan["static_resources"]
    assert [node["id"] for node in plan["nodes"]] == ["child.build", "summarize"]
    assert static_resources[0]["resolved_path"] == imported_context_path.as_posix()
    assert "secret-from-import" not in json.dumps(plan)
    assert "imported module context" in summarize_result
    assert "secret-from-import" in summarize_result

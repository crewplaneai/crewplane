from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path

from rich.console import Console

from orchestrator_cli.cli.workflow_runner import execute_workflow_run
from orchestrator_cli.core.config import (
    AgentConfig,
    Config,
    IntegrationsConfig,
    IntegrationSpec,
    Settings,
)
from orchestrator_cli.core.preflight import load_workflow_source_for_preflight
from orchestrator_cli.versions import (
    CONFIG_SCHEMA_VERSION,
    WORKFLOW_SCHEMA_VERSION,
)


def _config() -> Config:
    return Config(
        version=CONFIG_SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["mock"])},
        settings=Settings(
            integrations=IntegrationsConfig(
                invoker=IntegrationSpec(
                    implementation="mock",
                    options={"output_mode": "echo", "observation_delay_seconds": 0},
                ),
                artifacts=IntegrationSpec(
                    implementation="filesystem",
                    options={"allowed_template_paths": [], "log_cli_output": True},
                ),
                ui=IntegrationSpec(implementation="none", options={}),
            )
        ),
    )


def _workflow_text() -> str:
    return "\n".join(
        [
            "---",
            f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
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
            f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
            "name: Imported Child",
            "nodes:",
            "  - id: build",
            "    mode: sequential",
            "    providers: [alpha]",
            "---",
            "",
            "## build",
            "",
            "File: {{file:context.md}}",
            "Env: {{env:PREFLIGHT_IMPORTED_TOKEN}}",
            "Var: {{var:project_name}}",
        ]
    )


def _root_import_workflow_text() -> str:
    return "\n".join(
        [
            "---",
            f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
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
    stages_root = root / ".orchestrator" / "execution-stages"
    return sorted(path for path in stages_root.iterdir() if path.is_dir())


def test_mock_invoker_executes_preflight_plan_with_static_and_node_refs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workflow_path = tmp_path / ".orchestrator" / "workflows" / "workflow.task.md"
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
    result_dir = tmp_path / ".orchestrator" / "execution-results" / run_dir.name
    review_result = (result_dir / "review-result.md").read_text(encoding="utf-8")

    assert plan["plan_schema_version"] == "1.0"
    assert "secret-from-env" not in json.dumps(plan)
    assert any(entry["token_kind"] == "file" for entry in token_catalog)
    assert any(entry["token_kind"] == "env" for entry in token_catalog)
    assert any(entry["token_kind"] == "var" for entry in token_catalog)
    assert any(entry["token_kind"] == "node" for entry in token_catalog)
    assert "module context" in review_result
    assert os.environ["PREFLIGHT_PLAN_EXECUTION_TOKEN"] == "secret-from-env"


def test_mock_invoker_executes_imported_preflight_plan_with_module_file_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workflow_root = tmp_path / ".orchestrator" / "workflows"
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
    result_dir = tmp_path / ".orchestrator" / "execution-results" / run_dir.name
    summarize_result = (result_dir / "summarize-result.md").read_text(encoding="utf-8")

    static_resources = plan["static_resources"]
    assert [node["id"] for node in plan["nodes"]] == ["child.build", "summarize"]
    assert static_resources[0]["resolved_path"] == imported_context_path.as_posix()
    assert "secret-from-import" not in json.dumps(plan)
    assert "imported module context" in summarize_result
    assert "secret-from-import" in summarize_result

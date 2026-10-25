import asyncio
import json
import shutil
from pathlib import Path

from crewplane.cli.run.execution import execute_workflow_run
from crewplane.cli.run.preflight import compile_workflow_preview
from crewplane.cli.templates import render_template_content
from crewplane.core.config import Config
from crewplane.core.preflight.source import load_workflow_source_for_preflight
from crewplane.version import SCHEMA_VERSION
from tests.helpers.cli_provider import quiet_console


def test_bundled_text_example_renders_and_hands_off_content(tmp_path):
    templates = (
        Path(__file__).resolve().parents[3]
        / "src/crewplane/example_templates/example-templates"
    )
    target = tmp_path / ".crewplane/workflows/example-templates"
    shutil.copytree(templates / "sample-inputs", target / "sample-inputs")
    workflow = target / "text-generation-example.task.md"
    workflow.write_text(
        render_template_content((templates / workflow.name).read_text())
    )
    config = Config(
        version=SCHEMA_VERSION,
        agents={
            "local": {
                "cli_cmd": ["crewplane-ollama-agent"],
                "invocation_timeout_seconds": 900,
                "provider_kind": "ollama",
                "default_model": "provisioned-text-model",
                "prompt_transport": "stdin",
            }
        },
        settings={
            "integrations": {
                "invoker": {
                    "implementation": "mock",
                    "options": {"output_mode": "echo", "observation_delay_seconds": 0},
                },
                "ui": {"implementation": "none"},
            }
        },
    )
    source = load_workflow_source_for_preflight(workflow, tmp_path)
    preview = compile_workflow_preview(
        config,
        source,
        quiet_console(),
        True,
        "read_only",
        project_root=tmp_path,
        check_cli_availability=True,
    )
    assert not preview.has_errors(), preview.diagnostics
    assert not (tmp_path / ".crewplane/execution-stages").exists()
    asyncio.run(
        execute_workflow_run(
            config, source, False, True, quiet_console(), project_root=tmp_path
        )
    )
    stages = next((tmp_path / ".crewplane/execution-stages").iterdir())
    manifest = json.loads((stages / "manifests/run.json").read_text())
    assert manifest["status"] == "succeeded"
    results = tmp_path / ".crewplane/execution-results" / stages.name
    upstream = (results / "upstream-result.md").read_text()
    refined = (results / "refine-result.md").read_text()
    brief = (target / "sample-inputs/feature-brief.md").read_text()
    assert brief.strip() in upstream
    assert upstream.strip() in refined

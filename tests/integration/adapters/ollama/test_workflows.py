import asyncio
import json
import os
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.cli.app import app
from crewplane.cli.run.execution import execute_workflow_run
from crewplane.cli.run.invoker_preflight import collect_invoker_availability_diagnostics
from crewplane.cli.run.preflight import compile_workflow_preview
from crewplane.core.config import AgentConfig
from crewplane.core.review_contract import render_no_findings_review_contract
from crewplane.core.workflow.models import ProviderSpec
from crewplane.version import SCHEMA_VERSION
from tests.helpers.cli_provider import (
    command_receipts,
    provider_config,
    provider_source,
    quiet_console,
    wait_for_receipt,
)
from tests.helpers.platforms import requires_posix, requires_resume_support


@pytest.fixture
def run_clock():
    with patch("crewplane.artifacts.directory_manager.datetime") as clock:
        clock.now.side_effect = [
            datetime(2026, 10, 14) + timedelta(seconds=index) for index in range(20)
        ]
        yield


def config_for(ollama):
    return provider_config(
        AgentConfig(
            cli_cmd=[str(ollama.executable)],
            provider_kind="ollama",
            invocation_timeout_seconds=60,
            default_model="default-model",
        ),
        ollama.options,
    )


def run(config, source, root, force=False):
    source = replace(source, root_workflow_path=root / "workflow.task.md")
    return execute_workflow_run(
        config, source, force, True, quiet_console(), project_root=root
    )


def manifests(root):
    return [
        json.loads(path.read_text())
        for path in sorted(
            (root / ".crewplane/execution-stages").glob("*/manifests/run.json")
        )
    ]


def execution_receipts(ollama):
    return command_receipts(ollama.receipts, "run")


def test_validation_and_dry_run_are_artifact_free(ollama, tmp_path, monkeypatch):
    config = config_for(ollama)
    source = provider_source("override")
    dry = compile_workflow_preview(
        config, source, quiet_console(), True, "read_only", project_root=tmp_path
    )
    assert not dry.has_errors()
    assert not list(ollama.receipts.iterdir())
    for scenario, expected_error in [
        ("success", False),
        ("check-fail", True),
        ("partial", True),
    ]:
        monkeypatch.setenv("CREWPLANE_FAKE_SCENARIO", scenario)
        preview = compile_workflow_preview(
            config,
            source,
            quiet_console(),
            True,
            "read_only",
            project_root=tmp_path,
            check_cli_availability=True,
        )
        assert preview.has_errors() == expected_error
        assert not (tmp_path / ".crewplane").exists()
    assert all(
        receipt["argv"][-1] == "override"
        for receipt in command_receipts(ollama.receipts, "check")
    )


@pytest.mark.parametrize("executable_form", ["absolute", "path"])
def test_workflow_execution_resolves_symlinked_executable(
    ollama, tmp_path, monkeypatch, executable_form
):
    link = tmp_path / "linked-ollama"
    link.symlink_to(ollama.executable)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    config = config_for(ollama)
    config.agents["local"].cli_cmd = [
        str(link) if executable_form == "absolute" else link.name,
    ]

    asyncio.run(run(config, provider_source(), tmp_path))

    receipts = execution_receipts(ollama)
    assert len(receipts) == 1
    assert receipts[0]["argv"][0] == str(ollama.executable)
    assert manifests(tmp_path)[0]["status"] == "succeeded"


@pytest.mark.parametrize("search_entry", [".", "", "bin"])
@pytest.mark.parametrize("in_project", [True, False])
def test_full_preflight_resolves_relative_path_entries_against_project(
    ollama, tmp_path, monkeypatch, search_entry, in_project
):
    project = tmp_path / "project"
    caller = tmp_path / "caller"
    project.mkdir()
    caller.mkdir()
    executable_directory = (project if in_project else caller) / search_entry
    executable_directory.mkdir(exist_ok=True)
    (executable_directory / "ollama-agent").symlink_to(ollama.executable)
    monkeypatch.chdir(caller)
    monkeypatch.setenv("PATH", search_entry)
    config = config_for(ollama)
    config.agents["local"].cli_cmd = ["ollama-agent"]
    source = provider_source("override")

    preview = compile_workflow_preview(
        config,
        source,
        quiet_console(),
        True,
        "read_only",
        project_root=project,
        check_cli_availability=True,
    )
    canonical = build_runtime_config_snapshot(
        config, quiet_console(), True
    ).invoker_options
    diagnostics = asyncio.run(
        collect_invoker_availability_diagnostics(
            source.workflow, config, project, canonical
        )
    )

    assert preview.has_errors() is not in_project
    assert bool(diagnostics.errors) is not in_project
    if in_project:
        receipts = command_receipts(ollama.receipts, "check")
        assert len(receipts) == 2
        assert all(item["argv"][0] == str(ollama.executable) for item in receipts)
        assert all(item["argv"][-1] == "override" for item in receipts)
    else:
        assert any("not found in PATH" in error for error in diagnostics.errors)
        assert not list(ollama.receipts.iterdir())
    assert not (project / ".crewplane").exists()


@pytest.mark.parametrize("command", ["validate", "run"])
def test_cli_resolves_relative_path_entries_against_project(
    ollama, tmp_path, monkeypatch, command
):
    project = tmp_path / "project"
    caller = tmp_path / "caller"
    executable_directory = project / "bin"
    executable_directory.mkdir(parents=True)
    caller.mkdir()
    (executable_directory / "ollama-agent").symlink_to(ollama.executable)
    state_dir = project / ".crewplane"
    state_dir.mkdir()
    config = config_for(ollama)
    config.agents["local"].cli_cmd = ["ollama-agent"]
    config_path = state_dir / "config.yml"
    config_path.write_text(json.dumps(config.model_dump(mode="json")))
    workflow = project / "workflow.task.md"
    workflow.write_text(
        f"---\nschema_version: '{SCHEMA_VERSION}'\nname: relative-path\nnodes:\n  - id: text\n    mode: sequential\n    providers: [local]\n---\n## text\nhello\n"
    )
    monkeypatch.chdir(caller)
    monkeypatch.setenv("PATH", "bin")

    args = [command, "--config", str(config_path)]
    if command == "run":
        args.extend(["--tasks", str(workflow), "--no-live"])
    else:
        args.append(str(workflow))
    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, (result.output, result.exception)
    assert command_receipts(ollama.receipts, "check")
    receipts = command_receipts(ollama.receipts, "run")
    assert bool(receipts) == (command == "run")
    assert all(item["argv"][0] == str(ollama.executable) for item in receipts)
    assert all(item["cwd"] == str(project) for item in receipts)
    if command == "validate":
        assert list(state_dir.iterdir()) == [config_path]
    else:
        assert manifests(project)[0]["status"] == "succeeded"
        assert execution_receipts(ollama)[0]["stdin"] == "hello\n"
    assert not (caller / ".crewplane").exists()


@pytest.mark.usefixtures("run_clock")
def test_blackboard_handoff_dedupe_force_and_identity_changes(ollama, tmp_path):
    config = config_for(ollama)
    source = provider_source(downstream=True)
    asyncio.run(run(config, source, tmp_path))
    receipts = execution_receipts(ollama)
    assert len(receipts) == 2
    assert any(
        item["stdin"].startswith("Summarize") and "answer: hello" in item["stdin"]
        for item in receipts
    )
    original = manifests(tmp_path)[0]["workflow_signature"]
    assert manifests(tmp_path)[0]["status"] == "succeeded"
    assert list(
        (tmp_path / ".crewplane/execution-results").glob("*/downstream-result.md")
    )
    settings_path = tmp_path / ".crewplane" / "ollama.yml"
    settings_path.write_text("readiness_timeout_seconds: 120\n")
    asyncio.run(run(config, source, tmp_path))
    assert len(execution_receipts(ollama)) == 2
    assert len(manifests(tmp_path)) == 1
    asyncio.run(run(config, source, tmp_path, force=True))
    assert len(execution_receipts(ollama)) == 4
    metadata = tmp_path / "crewplane_ollama-0.2.0.dist-info/METADATA"
    metadata.write_text("Name: crewplane-ollama\nVersion: 0.3.0\n")
    asyncio.run(run(config, source, tmp_path))
    assert len(execution_receipts(ollama)) == 6
    assert manifests(tmp_path)[-1]["workflow_signature"] != original


@pytest.mark.usefixtures("run_clock")
def test_probe_failure_precedes_dedupe_and_writes_failure_bundle(
    ollama, tmp_path, monkeypatch
):
    config = config_for(ollama)
    source = provider_source()
    asyncio.run(run(config, source, tmp_path))
    monkeypatch.setenv("CREWPLANE_FAKE_SCENARIO", "check-fail")
    with pytest.raises(typer.Exit):
        asyncio.run(run(config, source, tmp_path))
    assert len(execution_receipts(ollama)) == 1
    assert list(
        (tmp_path / ".crewplane/execution-stages").glob("*/preflight/diagnostics.json")
    )


@requires_resume_support
@pytest.mark.usefixtures("run_clock")
@pytest.mark.parametrize("change_identity", [False, True])
def test_completed_node_resume_is_bound_to_plugin_identity(
    ollama, tmp_path, monkeypatch, change_identity
):
    config = config_for(ollama)
    source = provider_source(downstream=True)
    monkeypatch.setenv("CREWPLANE_FAKE_SCENARIO", "downstream-fail")
    with pytest.raises((RuntimeError, typer.Exit)):
        asyncio.run(run(config, source, tmp_path))
    assert manifests(tmp_path)[0]["status"] == "failed"
    assert not list(
        (tmp_path / ".crewplane/execution-results").glob("*/downstream-result.md")
    )
    monkeypatch.setenv("CREWPLANE_FAKE_SCENARIO", "success")
    if change_identity:
        (tmp_path / "crewplane_ollama-0.2.0.dist-info/METADATA").write_text(
            "Name: crewplane-ollama\nVersion: 0.3.0\n"
        )
    asyncio.run(run(config, source, tmp_path))
    latest = manifests(tmp_path)[-1]
    assert latest["status"] == "succeeded"
    assert latest.get("resumed_nodes", []) == ([] if change_identity else ["upstream"])
    assert len(execution_receipts(ollama)) == (4 if change_identity else 3)


@requires_posix
@pytest.mark.race
def test_real_run_preflight_cancellation_cleans_before_return(
    ollama, tmp_path, monkeypatch
):
    monkeypatch.setenv("CREWPLANE_FAKE_SCENARIO", "hold-check")
    captures = tmp_path / "captures"
    captures.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(captures))

    async def scenario():
        task = asyncio.create_task(run(config_for(ollama), provider_source(), tmp_path))
        try:
            ready = await wait_for_receipt(ollama.receipts, "ready-*")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            for pid in (int(ready.name.removeprefix("ready-")), int(ready.read_text())):
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
            assert not list(captures.iterdir())
            assert not (tmp_path / ".crewplane").exists()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.usefixtures("run_clock")
@pytest.mark.parametrize("contract", ["findings", "review"])
@pytest.mark.parametrize("valid", [True, False])
def test_plugin_preserves_findings_and_review_contracts(
    ollama, tmp_path, monkeypatch, contract, valid
):
    config = config_for(ollama)
    source = provider_source(downstream=True)
    config.settings.sequential_consensus_on_exhaustion = "fatal"
    if contract == "findings":
        source.workflow.nodes[0].findings = True
        text = "Report\n<!-- findings -->\n- A deterministic finding.\n<!-- /findings -->\n"
    else:
        source.workflow.nodes[0].providers.append(
            ProviderSpec(provider="local", role="reviewer")
        )
        text = render_no_findings_review_contract()
    answer = tmp_path / "fixture-answer.txt"
    answer.write_text(text if valid else "invalid contract")
    monkeypatch.setenv("CREWPLANE_FAKE_SCENARIO", "fixture-answer")
    monkeypatch.setenv("CREWPLANE_FAKE_ANSWER", str(answer))
    if valid:
        asyncio.run(run(config, source, tmp_path))
        assert manifests(tmp_path)[0]["status"] == "succeeded"
    else:
        with pytest.raises((RuntimeError, typer.Exit)):
            asyncio.run(run(config, source, tmp_path))
        assert manifests(tmp_path)[0]["status"] == "failed"
        assert not list(
            (tmp_path / ".crewplane/execution-results").glob("*/downstream-result.md")
        )

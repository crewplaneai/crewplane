import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from crewplane.cli.app import app
from crewplane.core.config import AgentConfig
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.cli_provider import OLLAMA_PACKAGE, provider_config, wait_for_receipt
from tests.helpers.platforms import requires_posix
from tests.helpers.processes import kill_process_group


@requires_posix
@pytest.mark.race
@pytest.mark.parametrize("command", ["validate", "run"])
@pytest.mark.parametrize("cleanup_confirmed", [True, False])
def test_isolated_sigint_awaits_and_reports_probe_cleanup(
    ollama, tmp_path, monkeypatch, command, cleanup_confirmed
):
    monkeypatch.setenv("CREWPLANE_FAKE_SCENARIO", "hold-run")
    config = provider_config(
        AgentConfig(
            cli_cmd=[str(ollama.executable), "run"],
            provider_kind="ollama",
            default_model="text-model",
        ),
        ollama.options,
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config.model_dump(mode="json")))
    workflow = tmp_path / "workflow.task.md"
    workflow.write_text(
        f"---\nschema_version: '{SCHEMA_VERSION}'\nname: probe-interrupt\nnodes:\n  - id: text\n    mode: sequential\n    providers: [local]\n---\n## text\nhello\n"
    )
    captures = tmp_path / "captures"
    captures.mkdir()
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(OLLAMA_PACKAGE / "src"), str(tmp_path)]),
        "TMPDIR": str(captures),
    }

    launcher = ["-c", "from crewplane.cli.app import app; app()"]
    if not cleanup_confirmed:
        fixture = (
            Path(__file__).resolve().parents[3]
            / "fixtures/providers/unconfirmed_probe_cleanup.py"
        )
        launcher = [str(fixture)]
    arguments = [command, "--config", str(config_path)]
    arguments.extend(
        [str(workflow)]
        if command == "validate"
        else ["--tasks", str(workflow), "--no-live"]
    )

    async def scenario():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            *launcher,
            *arguments,
            cwd=tmp_path,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            ready = await wait_for_receipt(ollama.receipts, "ready-*")
            process.send_signal(signal.SIGINT)
            stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
            assert process.returncode == (130 if cleanup_confirmed else 1), (
                stdout,
                stderr,
            )
            output = " ".join((stdout + stderr).decode().split())
            failure = "Provider process group could not be drained"
            if cleanup_confirmed:
                assert failure not in output
            else:
                pid = int(ready.name.removeprefix("ready-"))
                assert failure in output
                assert f"PID {pid}" in output
                assert f"process group {pid}" in output
            for pid in (int(ready.name.removeprefix("ready-")), int(ready.read_text())):
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
            assert not list(captures.iterdir())
            assert not (tmp_path / ".crewplane").exists()
        finally:
            for receipt in ollama.receipts.glob("ready-*"):
                kill_process_group(int(receipt.name.removeprefix("ready-")))
            if process.returncode is None:
                process.kill()
                await asyncio.wait_for(process.wait(), 10)

    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["validate", "run"])
def test_cli_reports_unconfirmed_probe_cleanup(ollama, tmp_path, monkeypatch, command):
    error = ProcessDrainError(
        ProcessDrainEvidence(42101, 42102, True, False),
        "Provider process group could not be drained within the finite TERM/KILL deadline.",
    )

    async def failed_probe(**kwargs):
        del kwargs
        try:
            raise asyncio.CancelledError from error
        except asyncio.CancelledError as exc:
            raise TimeoutError from exc

    monkeypatch.setattr(ollama.availability, "run_command_once", failed_probe)
    config = provider_config(
        AgentConfig(
            cli_cmd=[str(ollama.executable), "run"],
            provider_kind="ollama",
            default_model="text-model",
        ),
        ollama.options,
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config.model_dump(mode="json")))
    workflow = tmp_path / "workflow.task.md"
    workflow.write_text(
        f"---\nschema_version: '{SCHEMA_VERSION}'\nname: probe-cleanup\nnodes:\n  - id: text\n    mode: sequential\n    providers: [local]\n---\n## text\nhello\n"
    )

    arguments = [command, "--config", str(config_path)]
    arguments.extend(
        [str(workflow)]
        if command == "validate"
        else ["--tasks", str(workflow), "--no-live"]
    )
    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 1
    output = " ".join(result.output.split())
    assert str(error) in output
    assert "PID 42101" in output
    assert "process group 42102" in output
    assert not (tmp_path / ".crewplane").exists()

import asyncio
import os
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from crewplane.adapters.invokers.cli_invoker import (
    CliInvocationRequest,
    unconfirmed_process_cleanup,
)
from crewplane.adapters.invokers.cli_invoker.command_strategy import command_strategy
from crewplane.architecture.contracts import CommandResult
from crewplane.core.config import AgentConfig
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
)
from tests.helpers.cli_provider import command_receipts, wait_for_receipt
from tests.helpers.platforms import requires_posix


def request_for(ollama, root):
    return CliInvocationRequest(
        AgentConfig(cli_cmd=[str(ollama.executable), "run"], provider_kind="ollama"),
        "effective-model",
        command_strategy=command_strategy(),
        working_directory=root,
    )


def test_resolved_symlink_probes_exact_model_and_environment(
    ollama, tmp_path, monkeypatch
):
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:18999")
    link = tmp_path / "linked-cli"
    link.symlink_to(ollama.executable)
    request = request_for(ollama, tmp_path)
    request = replace(
        request,
        config=request.config.model_copy(update={"cli_cmd": ["./linked-cli", "run"]}),
    )
    asyncio.run(ollama.capability.check_availability(request))
    show = command_receipts(ollama.receipts, "show")[0]
    run = command_receipts(ollama.receipts, "run")[0]
    assert show["argv"] == [str(ollama.executable), "show", "effective-model"]
    assert run["argv"] == [
        str(ollama.executable),
        "run",
        "effective-model",
        "--nowordwrap",
        "--hidethinking",
    ]
    assert run["stdin"] == "Reply with OK only."
    assert show["host"] == run["host"] == "http://127.0.0.1:18999"
    assert not (tmp_path / ".crewplane").exists()


def test_relative_path_environment_is_resolved_against_request_cwd(ollama, tmp_path):
    request = request_for(ollama, tmp_path)
    request = replace(
        request,
        config=request.config.model_copy(update={"cli_cmd": ["ollama", "run"]}),
        environment={**os.environ, "PATH": "."},
    )
    asyncio.run(ollama.capability.check_availability(request))
    assert command_receipts(ollama.receipts, "show")[0]["argv"][0] == str(
        ollama.executable
    )


@pytest.mark.parametrize(
    "scenario,phase",
    [
        ("show-fail", "model lookup"),
        ("empty", "inference"),
        ("stderr", "inference"),
        ("partial", "inference"),
    ],
)
def test_probe_failures_identify_phase_and_model(
    ollama, tmp_path, monkeypatch, scenario, phase
):
    monkeypatch.setenv("CREWPLANE_FAKE_SCENARIO", scenario)
    captures = tmp_path / "captures"
    captures.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(captures))
    with pytest.raises(
        ValueError, match=f"provider 'ollama'.*effective-model.*{phase}"
    ):
        asyncio.run(ollama.capability.check_availability(request_for(ollama, tmp_path)))
    assert len(command_receipts(ollama.receipts, "run")) == (
        0 if scenario == "show-fail" else 1
    )
    assert not list(captures.iterdir())
    assert not (tmp_path / ".crewplane").exists()


@pytest.mark.parametrize("command", ["missing-ollama", "./missing", "."])
def test_missing_executables_cannot_launch_probe(ollama, tmp_path, command):
    request = request_for(ollama, tmp_path)
    request = replace(
        request, config=request.config.model_copy(update={"cli_cmd": [command, "run"]})
    )
    with pytest.raises(ValueError, match="executable resolution"):
        asyncio.run(ollama.capability.check_availability(request))
    assert not list(ollama.receipts.iterdir())


@pytest.mark.parametrize("phase", ["show", "run"])
def test_one_deadline_cancels_owned_probe_before_reporting(
    ollama, tmp_path, monkeypatch, phase
):
    cleaned = []

    async def command(**kwargs):
        if kwargs["cmd"][1] != phase:
            return CommandResult(0, "model", "")
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(phase)

    monkeypatch.setattr(ollama.availability, "run_command_once", command)
    monkeypatch.setattr(ollama.availability, "PROBE_TIMEOUT_SECONDS", 0.02)
    with pytest.raises(ValueError, match="deadline exceeded"):
        asyncio.run(ollama.capability.check_availability(request_for(ollama, tmp_path)))
    assert cleaned == [phase]


@pytest.mark.parametrize("failure", [PermissionError, TimeoutError])
def test_probe_deletes_all_captures_and_propagates_deletion_failure(
    ollama, tmp_path, monkeypatch, failure
):
    stdout, stderr = tmp_path / "stdout", tmp_path / "stderr"
    stdout.write_text("model")
    stderr.write_text("")

    async def command(**kwargs):
        assert kwargs["log_file"] is kwargs["invocation_context"] is None
        return CommandResult(0, "model", "", stdout, stderr)

    unlink = Path.unlink

    def fail_stdout(path, missing_ok=False):
        if path == stdout:
            raise failure("capture deletion failed")
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(ollama.availability, "run_command_once", command)
    monkeypatch.setattr(Path, "unlink", fail_stdout)
    with pytest.raises(failure, match="capture deletion failed"):
        asyncio.run(ollama.capability.check_availability(request_for(ollama, tmp_path)))
    assert not stderr.exists()


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.race
def test_cleanup_evidence_survives_deadline_and_cancellation(
    ollama, tmp_path, monkeypatch, cancel
):
    error = ProcessDrainError(
        ProcessDrainEvidence(123, 123, True, False), "cleanup unconfirmed"
    )

    async def scenario():
        ready = asyncio.Event()

        async def command(**kwargs):
            del kwargs
            ready.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                exc.add_note("cleanup unconfirmed")
                raise exc from error

        monkeypatch.setattr(ollama.availability, "run_command_once", command)
        monkeypatch.setattr(
            ollama.availability, "PROBE_TIMEOUT_SECONDS", 60 if cancel else 0.05
        )
        task = asyncio.create_task(
            ollama.capability.check_availability(request_for(ollama, tmp_path))
        )
        try:
            await asyncio.wait_for(ready.wait(), 2)
            if cancel:
                task.cancel()
            with pytest.raises(
                asyncio.CancelledError if cancel else TimeoutError
            ) as caught:
                await task
            assert unconfirmed_process_cleanup(caught.value) is error
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@requires_posix
@pytest.mark.race
def test_cancellation_drains_child_descendant_and_captures(
    ollama, tmp_path, monkeypatch
):
    monkeypatch.setenv("CREWPLANE_FAKE_SCENARIO", "hold-run")
    captures = tmp_path / "captures"
    captures.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(captures))

    async def scenario():
        task = asyncio.create_task(
            ollama.capability.check_availability(request_for(ollama, tmp_path))
        )
        try:
            ready = await wait_for_receipt(ollama.receipts, "ready-*")
            pids = [int(ready.name.removeprefix("ready-")), int(ready.read_text())]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            for pid in pids:
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
            assert not list(captures.iterdir())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

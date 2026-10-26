import asyncio
import os
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from crewplane.adapters.invokers.cli_invoker import (
    CliCommand,
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


def request_for(ollama, root, timeout=60):
    return CliInvocationRequest(
        AgentConfig(
            cli_cmd=[str(ollama.executable)],
            provider_kind="ollama",
            invocation_timeout_seconds=timeout,
        ),
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
        config=request.config.model_copy(update={"cli_cmd": ["./linked-cli"]}),
    )
    asyncio.run(ollama.capability.check_availability(request))
    check = command_receipts(ollama.receipts, "check")[0]
    assert check["argv"] == [
        str(ollama.executable),
        "--check",
        "--model",
        "effective-model",
    ]
    assert check["stdin"] == ""
    assert check["host"] == "http://127.0.0.1:18999"
    assert not command_receipts(ollama.receipts, "run")
    assert not (tmp_path / ".crewplane").exists()


def test_relative_path_environment_is_resolved_against_request_cwd(ollama, tmp_path):
    request = request_for(ollama, tmp_path)
    request = replace(
        request,
        config=request.config.model_copy(update={"cli_cmd": ["ollama-agent"]}),
        environment={**os.environ, "PATH": "."},
    )
    asyncio.run(ollama.capability.check_availability(request))
    assert command_receipts(ollama.receipts, "check")[0]["argv"][0] == str(
        ollama.executable
    )


@pytest.mark.parametrize(
    "scenario,phase",
    [
        ("check-fail", "readiness"),
        ("empty", "readiness"),
        ("stderr", "readiness"),
        ("partial", "readiness"),
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
    assert len(command_receipts(ollama.receipts, "check")) == 1
    assert not command_receipts(ollama.receipts, "run")
    assert not list(captures.iterdir())
    assert not (tmp_path / ".crewplane").exists()


@pytest.mark.parametrize("command", ["missing-ollama", "./missing", "."])
def test_missing_executables_cannot_launch_probe(ollama, tmp_path, command):
    request = request_for(ollama, tmp_path)
    request = replace(
        request, config=request.config.model_copy(update={"cli_cmd": [command]})
    )
    with pytest.raises(ValueError, match="executable resolution"):
        asyncio.run(ollama.capability.check_availability(request))
    assert not list(ollama.receipts.iterdir())


def test_one_deadline_cancels_owned_probe_before_reporting(
    ollama, tmp_path, monkeypatch
):
    cleaned = []

    async def command(**kwargs):
        assert kwargs["cmd"][1] == "--check"
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append("check")

    monkeypatch.setattr(ollama.availability, "run_command_once", command)
    with pytest.raises(ValueError, match="deadline exceeded"):
        asyncio.run(
            ollama.capability.check_availability(request_for(ollama, tmp_path, 0.02))
        )
    assert cleaned == ["check"]


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


@pytest.mark.parametrize(
    "stdout_path,stderr_path",
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_probe_cleanup_accepts_optional_and_missing_paths(
    ollama, tmp_path, monkeypatch, stdout_path, stderr_path
):
    async def command(**kwargs):
        del kwargs
        return CommandResult(
            0,
            "model",
            "",
            tmp_path / "missing-stdout" if stdout_path else None,
            tmp_path / "missing-stderr" if stderr_path else None,
        )

    monkeypatch.setattr(ollama.availability, "run_command_once", command)
    asyncio.run(
        ollama.availability._probe_command(
            request_for(ollama, tmp_path), CliCommand(["agent", "--check"], None)
        )
    )


def test_probe_cleanup_preserves_both_deletion_failures(ollama, tmp_path, monkeypatch):
    stdout, stderr = tmp_path / "stdout", tmp_path / "stderr"
    errors = {
        stdout: PermissionError("stdout cleanup"),
        stderr: OSError("stderr cleanup"),
    }
    deleted = []

    async def command(**kwargs):
        del kwargs
        return CommandResult(0, "model", "", stdout, stderr)

    def unlink(path, missing_ok=False):
        assert missing_ok
        deleted.append(path)
        raise errors[path]

    monkeypatch.setattr(ollama.availability, "run_command_once", command)
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(OSError) as caught:
        asyncio.run(ollama.capability.check_availability(request_for(ollama, tmp_path)))
    assert deleted == [stdout, stderr]
    assert caught.value is errors[stderr]
    assert caught.value.__context__ is errors[stdout]


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
        task = asyncio.create_task(
            ollama.capability.check_availability(
                request_for(ollama, tmp_path, 60 if cancel else 0.05)
            )
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
    monkeypatch.setenv("CREWPLANE_FAKE_SCENARIO", "hold-check")
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

import asyncio
import importlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from crewplane.adapters.invokers.cli_invoker import (
    CliDiscoveredEndpoint,
    CliDiscoveryRequest,
    unconfirmed_process_cleanup,
)
from crewplane.architecture.contracts import CommandResult
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
)
from tests.helpers.cli_provider import OLLAMA_PACKAGE, wait_for_receipt
from tests.helpers.platforms import requires_posix


@pytest.fixture
def discovery_process(ollama_package, tmp_path, monkeypatch):
    del ollama_package
    captures = tmp_path / "captures"
    captures.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(captures))
    return importlib.import_module("crewplane_ollama.discovery"), captures


@pytest.mark.parametrize("shadow_module", ["crewplane_ollama", "httpx"])
def test_discovery_worker_ignores_project_modules(
    discovery_process, tmp_path, monkeypatch, shadow_module
):
    module, captures = discovery_process
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    monkeypatch.setenv("PYTHONPATH", str(OLLAMA_PACKAGE / "src"))
    marker = tmp_path / "project-module-imported"
    (tmp_path / f"{shadow_module}.py").write_text(
        "from pathlib import Path\n"
        "Path('project-module-imported').touch()\n"
        "raise RuntimeError('Project module imported by discovery worker')\n",
        encoding="utf-8",
    )

    async def run():
        requests = []

        async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                requests.append(header.split(b" ")[1])
                body = b'{"version":"test"}'
                writer.write(
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        async with asyncio.TaskGroup() as connections:

            def accept(reader, writer):
                connections.create_task(respond(reader, writer))

            server = await asyncio.start_server(accept, "127.0.0.1", 0)
            async with server:
                endpoint = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                result = await module.discover(
                    CliDiscoveryRequest(tmp_path, {"OLLAMA_HOST": endpoint}, {}.get)
                )
                assert tuple(item.label for item in result.endpoints) == (endpoint,), (
                    result.diagnostics
                )
        assert requests == [b"/api/version"]

    asyncio.run(asyncio.wait_for(run(), 30))
    assert not marker.exists()
    assert not list(captures.iterdir())


@requires_posix
@pytest.mark.race
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("operation", ["endpoints", "models"])
def test_stalled_dns_worker_is_reaped_before_returning(
    discovery_process, tmp_path, monkeypatch, cancel, operation
):
    module, captures = discovery_process
    fixture = Path(__file__).parents[3] / "fixtures/providers/ollama_discovery_dns.py"
    run_command = module.run_command_once

    async def instrumented_command(**kwargs):
        kwargs["cmd"] = [sys.executable, str(fixture), str(tmp_path), kwargs["cmd"][-1]]
        return await run_command(**kwargs)

    monkeypatch.setattr(module, "run_command_once", instrumented_command)
    endpoint = "http://slow-resolver.invalid:11434"
    request = CliDiscoveryRequest(
        tmp_path, {"OLLAMA_HOST": endpoint}, {}.get, 60 if cancel else 3
    )

    async def run():
        if operation == "endpoints":
            task = asyncio.create_task(module.discover(request))
        else:
            task = asyncio.create_task(
                module.list_models(
                    request, CliDiscoveredEndpoint(endpoint, {"OLLAMA_HOST": endpoint})
                )
            )
        try:
            receipt = await wait_for_receipt(tmp_path, "ready-*")
            pid = int(receipt.name.removeprefix("ready-"))
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                result = await task
                diagnostic = " ".join(result.diagnostics)
                assert "timed out after 3s" in diagnostic
                assert "--discovery-timeout" in diagnostic
                assert "No installed" not in diagnostic
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            assert not list(captures.iterdir())
            assert not (tmp_path / ".crewplane").exists()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(asyncio.wait_for(run(), 12))


def test_real_worker_handles_slow_http_with_requested_budget(
    discovery_process, tmp_path
):
    module, captures = discovery_process

    async def run():
        paths = []
        connections = set()

        async def handle(reader, writer):
            connection = asyncio.current_task()
            connections.add(connection)
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                path = header.split(b" ")[1].decode()
                paths.append(path)
                if path == "/api/version":
                    # Exceed the old fixed two-second HTTP cutoff after receiving the request.
                    await asyncio.sleep(2.1)
                    data = {"version": "1"}
                elif path == "/api/tags":
                    data = {"models": [{"name": "local-text"}]}
                else:
                    data = {"capabilities": ["completion"]}
                body = json.dumps(data).encode()
                writer.write(
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                connections.discard(connection)

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        try:
            async with server:
                port = server.sockets[0].getsockname()[1]
                endpoint = f"http://localhost:{port}"
                request = CliDiscoveryRequest(
                    tmp_path, {"OLLAMA_HOST": endpoint}, {}.get, 10
                )
                result = await module.discover(request)
                assert [item.label for item in result.endpoints] == [endpoint]
                models = await module.list_models(request, result.endpoints[0])
                assert models.models == ("local-text",)
                assert models.diagnostics == ()
        finally:
            for connection in connections:
                connection.cancel()
            await asyncio.gather(*connections, return_exceptions=True)
        assert paths == ["/api/version", "/api/tags", "/api/show"]

    asyncio.run(asyncio.wait_for(run(), 15))
    assert not list(captures.iterdir())


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.race
def test_discovery_preserves_unconfirmed_cleanup(
    discovery_process, tmp_path, monkeypatch, cancel
):
    module, _ = discovery_process
    error = ProcessDrainError(
        ProcessDrainEvidence(123, 123, True, False), "cleanup unconfirmed"
    )

    async def run():
        ready = asyncio.Event()

        async def command(**kwargs):
            del kwargs
            ready.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                raise exc from error

        monkeypatch.setattr(module, "run_command_once", command)
        request = CliDiscoveryRequest(tmp_path, {}, {}.get, 60 if cancel else 0.05)
        task = asyncio.create_task(module.discover(request))
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

    asyncio.run(run())


@pytest.mark.parametrize("status,stdout", [(1, ""), (0, "invalid json")])
def test_failed_worker_cleans_captures(
    discovery_process, tmp_path, monkeypatch, status, stdout
):
    module, captures = discovery_process
    out, err = captures / "stdout", captures / "stderr"
    out.write_text(stdout)
    err.write_text("worker failed")

    async def command(**kwargs):
        Path(kwargs["cmd"][-1]).write_text(stdout)
        return CommandResult(status, stdout, "worker failed", out, err)

    monkeypatch.setattr(module, "run_command_once", command)
    with pytest.raises(ValueError):
        asyncio.run(module.discover(CliDiscoveryRequest(tmp_path, {}, {}.get)))
    assert not list(captures.iterdir())


@pytest.mark.parametrize(
    "stdout_path,stderr_path",
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_discovery_cleanup_accepts_optional_and_missing_paths(
    discovery_process, tmp_path, monkeypatch, stdout_path, stderr_path
):
    module, captures = discovery_process

    async def command(**kwargs):
        Path(kwargs["cmd"][-1]).write_text('{"values": [], "diagnostics": []}')
        return CommandResult(
            0,
            "",
            "",
            captures / "missing-stdout" if stdout_path else None,
            captures / "missing-stderr" if stderr_path else None,
        )

    monkeypatch.setattr(module, "run_command_once", command)
    asyncio.run(module.discover(CliDiscoveryRequest(tmp_path, {}, {}.get)))
    assert not list(captures.iterdir())


def test_discovery_cleanup_preserves_both_deletion_failures(
    discovery_process, tmp_path, monkeypatch
):
    module, captures = discovery_process
    stdout, stderr = captures / "stdout", captures / "stderr"
    errors = {
        stdout: PermissionError("stdout cleanup"),
        stderr: OSError("stderr cleanup"),
    }
    deleted = []
    original_unlink = Path.unlink

    async def command(**kwargs):
        del kwargs
        return CommandResult(0, "", "", stdout, stderr)

    def unlink(path, missing_ok=False):
        if path not in errors:
            return original_unlink(path, missing_ok=missing_ok)
        assert missing_ok
        deleted.append(path)
        raise errors[path]

    monkeypatch.setattr(module, "run_command_once", command)
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(OSError) as caught:
        asyncio.run(module.discover(CliDiscoveryRequest(tmp_path, {}, {}.get)))
    assert deleted == [stdout, stderr]
    assert caught.value is errors[stderr]
    assert caught.value.__context__ is errors[stdout]


def test_worker_protocol_uses_utf8_independent_of_pipe_encoding(
    ollama_package, tmp_path, monkeypatch
):
    del ollama_package
    worker = importlib.import_module("crewplane_ollama.discovery_worker")
    endpoint = "http://café.invalid:11434"
    request = worker.DiscoveryTask(
        operation="endpoints", endpoints=(endpoint,), timeout_seconds=15
    )
    incoming = io.BytesIO(request.model_dump_json().encode("utf-8"))
    report_path = tmp_path / "report.json"

    async def query(task, destination):
        assert destination == report_path
        assert task.endpoints == (endpoint,)
        return worker.DiscoveryReport(values=(endpoint,))

    with monkeypatch.context() as patch:
        patch.setattr(worker, "query_server", query)
        patch.setattr(sys, "stdin", io.TextIOWrapper(incoming, encoding="ascii"))
        patch.setattr(sys, "argv", ["worker", str(report_path)])
        worker.main()
        report = worker.DiscoveryReport.model_validate_json(report_path.read_bytes())
    assert report.values == (endpoint,)


@pytest.mark.parametrize(
    "operation,stalled_first",
    [("endpoints", False), ("endpoints", True), ("models", False)],
)
@pytest.mark.race
def test_deadline_preserves_completed_discovery_results(
    discovery_process, tmp_path, monkeypatch, operation, stalled_first
):
    module, captures = discovery_process

    async def run():
        stalled = asyncio.Event()
        connections = set()

        async def handle(reader, writer):
            connection = asyncio.current_task()
            connections.add(connection)
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                path = header.split(b" ")[1]
                if path == b"/api/show":
                    length = next(
                        int(line.split(b":")[1])
                        for line in header.lower().split(b"\r\n")
                        if line.startswith(b"content-length:")
                    )
                    payload = json.loads(await reader.readexactly(length))
                    should_stall = payload["model"] == "b"
                    data = {"capabilities": ["completion"]}
                else:
                    should_stall = (
                        operation == "endpoints" and b"Host: 127.0.0.1" in header
                    )
                    data = {"version": "1", "models": [{"name": "a"}, {"name": "b"}]}
                if should_stall:
                    stalled.set()
                    await reader.read()
                    return
                body = json.dumps(data).encode()
                writer.write(
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                connections.discard(connection)

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        task = None
        try:
            async with server:
                port = server.sockets[0].getsockname()[1]
                endpoint = f"http://localhost:{port}"
                candidates = (endpoint, f"http://127.0.0.1:{port}")
                if stalled_first:
                    candidates = tuple(reversed(candidates))

                def endpoint_candidates(request):
                    del request
                    return candidates

                monkeypatch.setattr(module, "endpoint_candidates", endpoint_candidates)
                request = CliDiscoveryRequest(tmp_path, {}, {}.get, 3)
                if operation == "endpoints":
                    task = asyncio.create_task(module.discover(request))
                else:
                    task = asyncio.create_task(
                        module.list_models(
                            request,
                            CliDiscoveredEndpoint(endpoint, {"OLLAMA_HOST": endpoint}),
                        )
                    )
                await asyncio.wait_for(stalled.wait(), 5)
                result = await task
                assert "timed out" in " ".join(result.diagnostics)
                if operation == "endpoints":
                    assert tuple(item.label for item in result.endpoints) == (endpoint,)
                else:
                    assert result.models == ("a",)
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            for connection in connections:
                connection.cancel()
            await asyncio.gather(*connections, return_exceptions=True)

    asyncio.run(asyncio.wait_for(run(), 12))
    assert not list(captures.iterdir())

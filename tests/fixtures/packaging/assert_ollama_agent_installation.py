"""Exercise the installed agent using a local server and real command effects."""

import asyncio
import importlib.metadata
import json
import os
import shutil
import sys
from functools import partial
from pathlib import Path

from crewplane_ollama.discovery_platform import discover_client

from crewplane.adapters.invokers.cli_invoker import CliDiscoveryRequest

MODEL = "installed-tool-model"
PROMPT = "Implement the café check without leaking intermediate output."
THINKING = "private-thought-not-a-log"


def assistant(content, calls=None):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message.update(tool_calls=calls, thinking=THINKING)
    return {"done": True, "done_reason": "stop", "message": message}


class InstalledAgentServer:
    def __init__(self, workspace):
        self.workspace = workspace
        self.requests = []
        self.rounds = 0

    async def respond(self, reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.split(b":", 1)[1])
                for line in header.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            body = json.loads(await reader.readexactly(length))
            self.requests.append((header.split(b" ")[1], body))
            result = self.response(header.split(b" ")[1], body)
            encoded = json.dumps(result).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(encoded)}\r\nConnection: close\r\n\r\n".encode()
                + encoded
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def response(self, path, body):
        assert body["model"] == MODEL
        if path == b"/api/show":
            return {"capabilities": ["completion", "tools"]}
        assert path == b"/api/chat"
        assert body["stream"] is False
        assert body["truncate"] is False
        assert body["shift"] is False
        if "tools" not in body:
            assert body["messages"][-1]["content"] == "Reply with OK only."
            return assistant("OK")
        assert len(body["tools"]) == 1
        assert body["tools"][0]["function"]["name"] == "run_command"
        assert body["messages"][1]["content"] == PROMPT
        self.rounds += 1
        if self.rounds == 1:
            program = (
                "import json, os, pathlib, sys; "
                "pathlib.Path('created file.txt').write_text(sys.stdin.read(), encoding='utf-8'); "
                "print(json.dumps({'cwd': os.getcwd(), 'marker': os.environ['PACKAGE_AGENT_MARKER']}))"
            )
            return assistant(
                "intermediate-not-the-final-answer",
                [
                    {
                        "id": "installed-call",
                        "function": {
                            "name": "run_command",
                            "arguments": {
                                "argv": [sys.executable, "-c", program],
                                "stdin": "café from installed tool\n",
                            },
                        },
                    }
                ],
            )
        assert self.rounds == 2
        previous = body["messages"][-2]
        assert previous["thinking"] == THINKING
        tool = body["messages"][-1]
        assert tool["role"] == "tool"
        assert tool["tool_name"] == "run_command"
        assert tool["tool_call_id"] == "installed-call"
        result = json.loads(tool["content"])
        assert result["exit_code"] == 0, result
        assert json.loads(result["stdout"]) == {
            "cwd": str(self.workspace),
            "marker": "environment-inherited",
        }
        assert (self.workspace / "created file.txt").read_text(encoding="utf-8") == (
            "café from installed tool\n"
        )
        return assistant("Installed café feature verified.")


async def launch_agent(executable, workspace, environment, check):
    command = [str(executable), "--model", MODEL]
    if check:
        command.append("--check")
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=workspace,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await process.communicate(
            b"" if check else PROMPT.encode("utf-8")
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, stderr.decode("utf-8", "replace")
    assert THINKING.encode() not in stdout + stderr
    if not check:
        assert stdout.decode("utf-8").strip() == "Installed café feature verified."
    return stdout, stderr


async def check_installed_agent():
    binary = Path(sys.executable).parent
    name = "crewplane-ollama-agent.exe" if os.name == "nt" else "crewplane-ollama-agent"
    executable = binary / name
    assert executable.is_file(), executable
    workspace = Path.cwd() / "workspace with spaces"
    workspace.mkdir()
    public_commands = Path.cwd() / "public commands"
    public_commands.mkdir()
    environment = {**os.environ, "PATH": str(public_commands)}
    environment.pop("PYTHONPATH", None)
    environment["PACKAGE_AGENT_MARKER"] = "environment-inherited"
    lookup = partial(shutil.which, path=environment["PATH"])
    assert lookup("ollama") is None
    assert lookup(name) is None
    command = discover_client(CliDiscoveryRequest(workspace, environment, lookup))
    assert command == (str(executable.resolve()),), command
    executable = Path(command[0])
    scripted = InstalledAgentServer(workspace)
    async with asyncio.timeout(30), asyncio.TaskGroup() as connections:

        def accept(reader, writer):
            connections.create_task(scripted.respond(reader, writer))

        server = await asyncio.start_server(accept, "127.0.0.1", 0)
        async with server:
            environment["OLLAMA_HOST"] = (
                f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            )
            await launch_agent(executable, workspace, environment, True)
            assert not (workspace / "created file.txt").exists()
            await launch_agent(executable, workspace, environment, False)
    assert [path for path, body in scripted.requests] == [
        b"/api/show",
        b"/api/chat",
        b"/api/show",
        b"/api/chat",
        b"/api/chat",
    ]


def main():
    distribution = importlib.metadata.distribution("crewplane-ollama")
    entry = next(
        entry
        for entry in distribution.entry_points
        if entry.name == "crewplane-ollama-agent"
    )
    assert entry.group == "console_scripts"
    assert entry.value == "crewplane_ollama.agent_cli:main"
    assert distribution.version == "0.2.0"
    files = {str(path) for path in distribution.files}
    for name in (
        "agent_cli.py",
        "agent.py",
        "protocol.py",
        "instructions.py",
        "command_executor.py",
        "limits.py",
        "models.py",
        "py.typed",
    ):
        assert f"crewplane_ollama/{name}" in files
    dependency = next(
        requirement
        for requirement in importlib.metadata.requires("crewplane")
        if requirement.startswith("crewplane-ollama")
    )
    assert ">=0.2.0" in dependency and "<0.3" in dependency, dependency
    asyncio.run(check_installed_agent())
    print("Installed Ollama entry point passed readiness and real tool execution.")


if __name__ == "__main__":
    main()

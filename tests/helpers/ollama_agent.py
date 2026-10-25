"""Real loopback HTTP and executable support for the plugin's agent contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.helpers.cli_provider import OLLAMA_PACKAGE


def final_response(content="final answer", **message):
    return {
        "done": True,
        "done_reason": "stop",
        "message": {"role": "assistant", "content": content, **message},
    }


def command_call(argv, identifier=None, **arguments):
    call = {
        "function": {"name": "run_command", "arguments": {"argv": argv, **arguments}}
    }
    if identifier is not None:
        call["id"] = identifier
    return call


def tool_response(*calls, **message):
    return final_response("", tool_calls=list(calls), **message)


class ScriptedOllama:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.shows = []
        self.details = {"capabilities": ["completion", "tools"]}

    def __call__(self, path, payload):
        if path == "/api/show":
            self.shows.append(payload)
            return self.details
        assert path == "/api/chat", path
        self.requests.append(payload)
        if "tools" not in payload:
            return final_response("OK")
        assert self.replies, "unexpected extra model request"
        reply = self.replies.pop(0)
        return reply(payload) if callable(reply) else reply


@contextmanager
def ollama_server(handler: Callable) -> Iterator[str]:
    failures: list[BaseException] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                response = handler(self.path, payload)
                status, content = (
                    response
                    if isinstance(response, tuple)
                    else (200, json.dumps(response).encode())
                )
            except BaseException as exc:
                failures.append(exc)
                status, content = 500, b"fixture failed"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(content)

        def log_message(self, format, *args):
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
    if failures:
        raise failures[0]


def invoke_agent(root, endpoint, prompt="task", check=False, extra_environment=None):
    environment = dict(os.environ)
    environment.update(
        OLLAMA_HOST=endpoint,
        PYTHONPATH=str(OLLAMA_PACKAGE / "src"),
    )
    environment.update(extra_environment or {})
    return subprocess.run(
        [
            sys.executable,
            "-P",
            "-m",
            "crewplane_ollama.agent_cli",
            "--model",
            "test-model",
            *(["--check"] if check else []),
        ],
        input=prompt.encode() if isinstance(prompt, str) else prompt,
        cwd=root,
        env=environment,
        capture_output=True,
        timeout=30,
        check=False,
    )


def agent_executable(root: Path) -> Path:
    if os.name != "posix":
        import pytest

        pytest.skip(
            "Source entry-point shim uses POSIX shebang; installed package checks cover launchers"
        )
    path = root / "agent executable"
    path.write_text(
        f"#!{sys.executable}\nfrom crewplane_ollama.agent_cli import main\nmain()\n"
    )
    path.chmod(0o755)
    return path

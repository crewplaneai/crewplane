import importlib
import json
import sys

import pytest

from tests.helpers.ollama_agent import (
    ScriptedOllama,
    command_call,
    final_response,
    invoke_agent,
    ollama_server,
    tool_response,
)

pytestmark = pytest.mark.usefixtures("ollama_package")


def test_exact_tool_budget_allows_final_answer(tmp_path):
    unknown = {"function": {"name": "unavailable", "arguments": {}}}

    def final(payload):
        results = [
            message for message in payload["messages"] if message["role"] == "tool"
        ]
        assert len(results) == 64
        assert all(json.loads(message["content"])["error"] for message in results)
        return final_response("64 attempts, final answer")

    script = ScriptedOllama([*(tool_response(unknown) for _ in range(64)), final])
    with ollama_server(script) as endpoint:
        result = invoke_agent(tmp_path, endpoint)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"64 attempts, final answer"
    assert len(script.requests) == 65


@pytest.mark.parametrize("used", [0, 63, 64])
def test_over_budget_batch_executes_none_of_that_batch(tmp_path, used):
    invalid = {"function": {"name": "unknown", "arguments": {}}}
    command = command_call(
        [sys.executable, "-c", "from pathlib import Path; Path('forbidden').touch()"]
    )
    replies = [tool_response(*([invalid] * used))] if used else []
    replies.append(tool_response(command, *([invalid] * (64 - used))))
    with ollama_server(ScriptedOllama(replies)) as endpoint:
        result = invoke_agent(tmp_path, endpoint)
    assert result.returncode != 0 and not result.stdout
    assert not (tmp_path / "forbidden").exists()
    assert b"budget" in result.stderr


def test_request_limit_counts_serialized_utf8_without_trimming():
    http = importlib.import_module("crewplane_ollama.discovery_http")
    content = {"messages": [{"content": "é" * (4 * 1024 * 1024)}]}
    with pytest.raises(ValueError, match="8 MiB"):
        http.serialize_request(content)
    assert content["messages"][0]["content"].endswith("é")


def test_history_limit_fails_before_sending_next_request(tmp_path):
    calls = [
        tool_response(
            {"function": {"name": "unknown", "arguments": {}}},
            thinking="x" * (1500 * 1024),
        )
        for _ in range(6)
    ]
    script = ScriptedOllama(calls)
    with ollama_server(script) as endpoint:
        result = invoke_agent(tmp_path, endpoint)
    assert result.returncode != 0 and not result.stdout
    assert len(script.requests) == 6
    assert b"8 MiB" in result.stderr
    assert len(result.stderr) < 8192


def test_command_diagnostics_are_bounded_and_do_not_dump_environment(tmp_path):
    script = ScriptedOllama(
        [
            tool_response(command_call([sys.executable, "-c", "print('x' * 100000)"])),
            final_response(),
        ]
    )
    with ollama_server(script) as endpoint:
        result = invoke_agent(
            tmp_path, endpoint, extra_environment={"DO_NOT_LOG": "secret-env-value"}
        )
    assert result.returncode == 0
    assert len(result.stderr) < 9000
    assert b"secret-env-value" not in result.stderr
    assert b"diagnostic truncated" in result.stderr

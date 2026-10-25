import asyncio
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


def test_native_multiple_calls_rounds_ids_thinking_and_final_only(tmp_path):
    def after_calls(payload):
        messages = payload["messages"]
        assert messages[-3]["content"] == "intermediate"
        assert messages[-3]["thinking"] == "private reasoning"
        assert json.loads(messages[-2]["content"])["stdout"] == "one\n"
        assert messages[-2]["tool_name"] == "run_command"
        assert messages[-2]["tool_call_id"] == "first"
        assert "tool_call_id" not in messages[-1]
        assert json.loads(messages[-1]["content"])["exit_code"] == 3
        return tool_response(command_call([sys.executable, "-c", "print('corrected')"]))

    script = ScriptedOllama(
        [
            tool_response(
                command_call([sys.executable, "-c", "print('one')"], "first"),
                command_call([sys.executable, "-c", "raise SystemExit(3)"]),
                thinking="private reasoning",
            ),
            after_calls,
            final_response("canonical ✓"),
        ]
    )
    script.replies[0]["message"]["content"] = "intermediate"
    with ollama_server(script) as endpoint:
        result = invoke_agent(tmp_path, endpoint, "unchanged UTF-8 π")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "canonical ✓".encode()
    assert b"private reasoning" not in result.stderr
    assert b"tool 1 start" in result.stderr and b"tool 3 result" in result.stderr
    for request in script.requests:
        assert request["model"] == "test-model"
        assert request["stream"] is request["shift"] is request["truncate"] is False
        assert "think" not in request
        assert len(request["tools"]) == 1
        assert request["messages"][1]["content"] == "unchanged UTF-8 π"


@pytest.mark.parametrize(
    "bad_call",
    [
        None,
        [],
        {},
        {"type": "other"},
        {"function": {}},
        {"function": {"name": "x", "arguments": "{}"}},
        {"function": {"name": "x", "arguments": []}},
        {"id": None, "function": {"name": "x", "arguments": {}}},
        {"function": {"name": "", "arguments": {}}},
    ],
)
def test_malformed_later_envelope_prevents_entire_batch(tmp_path, bad_call):
    sentinel = tmp_path / "not-created"
    script = ScriptedOllama(
        [
            tool_response(
                command_call(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('not-created').touch()",
                    ]
                ),
                bad_call,
            )
        ]
    )
    with ollama_server(script) as endpoint:
        result = invoke_agent(tmp_path, endpoint)
    assert result.returncode != 0 and not result.stdout
    assert not sentinel.exists()


@pytest.mark.parametrize(
    "response",
    [
        {},
        {
            "done": False,
            "done_reason": "stop",
            "message": {"role": "assistant", "content": "partial"},
        },
        {
            "done": True,
            "done_reason": "length",
            "message": {"role": "assistant", "content": "partial"},
        },
        final_response("  "),
        final_response(42),
        final_response("text", thinking=[]),
        final_response("text", tool_calls={}),
        {
            "done": True,
            "done_reason": "stop",
            "message": {"role": "tool", "content": "fake"},
        },
    ],
)
def test_incomplete_or_invalid_response_never_publishes(tmp_path, response):
    with ollama_server(ScriptedOllama([response])) as endpoint:
        result = invoke_agent(tmp_path, endpoint)
    assert result.returncode != 0 and not result.stdout


def test_unknown_tool_and_schema_errors_are_correctable_without_spawn(tmp_path):
    calls = [
        {"function": {"name": "unknown", "arguments": {}}},
        command_call(
            [sys.executable, "-c", "raise Exception('must not spawn')"], cwd="elsewhere"
        ),
    ]

    def inspect(payload):
        for message in payload["messages"][-2:]:
            feedback = json.loads(message["content"])
            assert feedback["exit_code"] is None and feedback["error"]
        return final_response("blocked honestly")

    with ollama_server(ScriptedOllama([tool_response(*calls), inspect])) as endpoint:
        result = invoke_agent(tmp_path, endpoint)
    assert result.returncode == 0 and result.stdout == b"blocked honestly"


def test_prose_commands_are_not_executed(tmp_path):
    answer = "```sh\ntouch forbidden\n```"
    with ollama_server(ScriptedOllama([final_response(answer)])) as endpoint:
        result = invoke_agent(tmp_path, endpoint)
    assert result.returncode == 0 and result.stdout.decode() == answer
    assert not (tmp_path / "forbidden").exists()


def test_response_roundtrip_retains_complete_native_message():
    protocol = importlib.import_module("crewplane_ollama.protocol")
    response = final_response(
        "hello", tool_calls=[], thinking="kept", images=["optional"]
    )
    assert protocol.parse_response(response).message == response["message"]
    assert "tools" not in protocol.chat_request("model", [], False)


def test_unknown_tool_uses_no_executor(monkeypatch):
    agent = importlib.import_module("crewplane_ollama.agent")
    protocol = importlib.import_module("crewplane_ollama.protocol")

    async def forbidden(arguments):
        raise AssertionError(arguments)

    monkeypatch.setattr(agent, "execute_command", forbidden)
    result = asyncio.run(agent.dispatch(protocol.ToolCall("not-available", {}), 1))
    assert result["exit_code"] is None and "Unknown tool" in result["error"]


@pytest.mark.parametrize("field", ["content", "thinking", "arguments"])
def test_unencodable_preserved_message_fails_before_any_command(tmp_path, field):
    response = tool_response(
        command_call(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('forbidden').touch()",
            ]
        )
    )
    if field == "arguments":
        response["message"]["tool_calls"].append(command_call(["\ud800"]))
    else:
        response["message"][field] = "\ud800"
    with ollama_server(ScriptedOllama([response])) as endpoint:
        result = invoke_agent(tmp_path, endpoint)
    assert result.returncode != 0 and not result.stdout
    assert not (tmp_path / "forbidden").exists()
    assert b"UTF-8" in result.stderr

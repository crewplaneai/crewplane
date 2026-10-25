import importlib
import io
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


@pytest.mark.parametrize(
    "prompt", [b"\xff", b"x" * (1024 * 1024 + 1)], ids=["invalid-utf8", "oversized"]
)
def test_invalid_prompt_fails_before_any_http_request(tmp_path, prompt):
    script = ScriptedOllama([])
    with ollama_server(script) as endpoint:
        result = invoke_agent(tmp_path, endpoint, prompt)
    assert result.returncode != 0 and not result.stdout
    assert not script.shows and not script.requests


def test_prompt_reader_is_bounded_and_utf8():
    cli = importlib.import_module("crewplane_ollama.agent_cli")
    assert cli.read_prompt(io.BytesIO("π".encode())) == "π"
    assert cli.read_prompt(io.BytesIO(b"x" * (1024 * 1024))) == "x" * (1024 * 1024)


def test_check_never_reads_stdin_or_dispatches_tools(tmp_path):
    requests = []

    def handler(path, payload):
        requests.append((path, payload))
        if path == "/api/show":
            return {"capabilities": ["completion", "tools"]}
        assert "tools" not in payload
        assert payload["messages"] == [
            {"role": "user", "content": "Reply with OK only."}
        ]
        return tool_response(
            command_call(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('forbidden').touch() ",
                ]
            )
        )

    with ollama_server(handler) as endpoint:
        result = invoke_agent(tmp_path, endpoint, b"\xff", check=True)
    assert result.returncode != 0 and not result.stdout
    assert not (tmp_path / "forbidden").exists()
    assert len(requests) == 2


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"capabilities": ["completion"]},
        {"capabilities": ["tools"]},
        {"capabilities": ["completion", "tools"], "remote_model": "cloud"},
        {"capabilities": ["completion", "tools"], "remote_host": "cloud"},
    ],
)
def test_execution_rechecks_eligibility_before_sending_prompt(tmp_path, metadata):
    script = ScriptedOllama([final_response()])
    with ollama_server(script) as endpoint:
        assert invoke_agent(tmp_path, endpoint, check=True).returncode == 0
        script.details = metadata
        result = invoke_agent(tmp_path, endpoint, "SECRET REPOSITORY PROMPT")
    assert result.returncode != 0 and not result.stdout
    assert len(script.requests) == 1
    assert "SECRET" not in json.dumps(script.shows)
    assert endpoint.encode() in result.stderr


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"value": NaN}', b"x" * (2 * 1024 * 1024 + 1)],
    ids=["invalid-json", "non-object", "nonfinite", "oversized"],
)
def test_invalid_http_body_is_never_an_answer(tmp_path, body):
    def handler(path, payload):
        del path, payload
        return 200, body

    with ollama_server(handler) as endpoint:
        result = invoke_agent(tmp_path, endpoint)
    assert result.returncode != 0 and not result.stdout


@pytest.mark.parametrize("status", [302, 404, 500, 503])
def test_http_error_has_no_retry_or_body_leak(tmp_path, status):
    requests = []

    def handler(path, payload):
        requests.append((path, payload))
        return status, b"private error body"

    with ollama_server(handler) as endpoint:
        result = invoke_agent(tmp_path, endpoint)
    assert result.returncode != 0 and not result.stdout
    assert b"private error body" not in result.stderr
    assert len(requests) == 1


def test_explicit_endpoint_and_proxy_environment_are_authoritative(tmp_path):
    script = ScriptedOllama([final_response("selected server")])
    with ollama_server(script) as endpoint:
        result = invoke_agent(
            tmp_path,
            endpoint,
            extra_environment={
                "HTTP_PROXY": "http://127.0.0.1:1",
                "HTTPS_PROXY": "http://127.0.0.1:1",
                "ALL_PROXY": "http://127.0.0.1:1",
                "NO_PROXY": "",
            },
        )
    assert result.returncode == 0 and result.stdout == b"selected server"
    failed = invoke_agent(tmp_path, "http://127.0.0.1:1")
    assert failed.returncode != 0 and not failed.stdout


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "ftp://localhost",
        "http://user:password@localhost",
        "http://localhost/path",
        "http://localhost?query",
        "http://localhost#fragment",
    ],
)
def test_invalid_selected_endpoint_has_no_fallback(tmp_path, endpoint):
    result = invoke_agent(tmp_path, endpoint)
    assert result.returncode != 0 and not result.stdout


def test_system_instruction_preserves_role_and_explicit_skill_contract(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    text = importlib.import_module("crewplane_ollama.instructions").system_instruction()
    assert str(tmp_path) in text
    for required in (
        "scoped AGENTS.md",
        "read-only",
        "SKILL.md",
        "source directory",
        "MCP",
        "host API",
        "blocker",
        "foreground",
        "environment",
        "not a sandbox",
    ):
        assert required in text


def test_unencodable_final_text_fails_without_canonical_output(tmp_path):
    with ollama_server(ScriptedOllama([final_response("\ud800")])) as endpoint:
        result = invoke_agent(tmp_path, endpoint)
    assert result.returncode != 0 and not result.stdout
    assert len(result.stderr) < 4096

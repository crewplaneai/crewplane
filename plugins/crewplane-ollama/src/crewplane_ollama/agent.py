"""One bounded conversation owned by one supervised provider process."""

from __future__ import annotations

import json
import os
import sys

import httpx

from .command_executor import execute_command
from .discovery_http import execution_client, read_json
from .discovery_platform import normalize_endpoint
from .instructions import system_instruction
from .limits import MAX_DIAGNOSTIC_CHARACTERS, MAX_TOOL_ATTEMPTS
from .models import validate_model
from .protocol import ToolCall, chat_request, parse_response, tool_message


def execution_endpoint() -> str:
    return normalize_endpoint(os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))


async def verify_model(client: httpx.AsyncClient, endpoint: str, model: str) -> None:
    try:
        details = await read_json(client, endpoint + "/api/show", {"model": model})
        validate_model(details, model)
    except ValueError as exc:
        raise ValueError(
            f"Model eligibility for {model!r} at {endpoint}: {exc}"
        ) from exc


async def check(model: str) -> str:
    endpoint = execution_endpoint()
    async with execution_client() as client:
        await verify_model(client, endpoint, model)
        data = await read_json(
            client,
            endpoint + "/api/chat",
            chat_request(
                model, [{"role": "user", "content": "Reply with OK only."}], False
            ),
        )
        response = parse_response(data)
        if response.calls:
            raise ValueError(
                "Ollama readiness must return an answer without tool calls"
            )
        return response.content


async def run(model: str, prompt: str) -> str:
    endpoint = execution_endpoint()
    async with execution_client() as client:
        await verify_model(client, endpoint, model)
        messages: list[dict[str, object]] = [
            {"role": "system", "content": system_instruction()},
            {"role": "user", "content": prompt},
        ]
        attempts = 0
        while True:
            data = await read_json(
                client, endpoint + "/api/chat", chat_request(model, messages)
            )
            response = parse_response(data)
            if not response.calls:
                return response.content
            if attempts + len(response.calls) > MAX_TOOL_ATTEMPTS:
                raise ValueError(
                    "Ollama tool-call budget exceeded (64 attempts); batch not executed"
                )
            messages.append(response.message)
            for call in response.calls:
                attempts += 1
                result = await dispatch(call, attempts)
                messages.append(tool_message(call, result))


async def dispatch(call: ToolCall, order: int) -> dict[str, object]:
    diagnostic(
        f"tool {order} start", {"tool": call.name, "argv": call.arguments.get("argv")}
    )
    result: dict[str, object] = (
        await execute_command(call.arguments)
        if call.name == "run_command"
        else {"error": f"Unknown tool: {call.name}", "exit_code": None}
    )
    metadata = {
        key: value for key, value in result.items() if key not in {"stdout", "stderr"}
    }
    diagnostic(
        f"tool {order} result",
        {
            **metadata,
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
        },
    )
    return result


def diagnostic(label: str, details: dict[str, object]) -> None:
    text = json.dumps(details, ensure_ascii=True, allow_nan=False)
    if len(text) > MAX_DIAGNOSTIC_CHARACTERS:
        text = text[:MAX_DIAGNOSTIC_CHARACTERS] + " [diagnostic truncated]"
    print(f"{label}: {text}", file=sys.stderr, flush=True)

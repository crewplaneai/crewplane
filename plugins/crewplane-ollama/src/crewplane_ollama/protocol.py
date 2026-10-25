"""Native Ollama envelopes; validate an entire batch before any side effects."""

from __future__ import annotations

import json
from dataclasses import dataclass

RUN_COMMAND_TOOL: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "Run a foreground command in the supplied workspace. Use an explicit "
            "interpreter or shell when needed. Obey the task's read-only and other restrictions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "stdin": {"type": "string"},
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, object]
    identifier: str | None = None


@dataclass(frozen=True)
class AssistantResponse:
    message: dict[str, object]
    content: str
    calls: tuple[ToolCall, ...]


def chat_request(
    model: str, messages: list[dict[str, object]], tools_enabled: bool = True
) -> dict[str, object]:
    request: dict[str, object] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "truncate": False,
        "shift": False,
    }
    if tools_enabled:
        request["tools"] = [RUN_COMMAND_TOOL]
    return request


def parse_response(data: dict[str, object]) -> AssistantResponse:
    if data.get("done") is not True or data.get("done_reason") != "stop":
        raise ValueError(
            "Ollama response did not complete normally (done/stop required)"
        )
    message = data.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ValueError("Ollama response must contain an assistant message")
    content = message.get("content")
    if not isinstance(content, str):
        raise ValueError("Ollama assistant content must be a string")
    if "thinking" in message and not isinstance(message["thinking"], str):
        raise ValueError("Ollama assistant thinking must be a string")
    try:
        json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "Ollama assistant message must contain valid UTF-8 text"
        ) from exc
    calls = message.get("tool_calls", [])
    if not isinstance(calls, list):
        raise ValueError("Ollama tool_calls must be an array")
    parsed = tuple(parse_tool_call(call) for call in calls)
    if not parsed and not content.strip():
        raise ValueError("Ollama final answer must contain nonblank content")
    return AssistantResponse(message, content, parsed)


def parse_tool_call(call: object) -> ToolCall:
    if not isinstance(call, dict) or call.get("type", "function") != "function":
        raise ValueError("Ollama tool call must be a function envelope")
    function = call.get("function")
    if not isinstance(function, dict):
        raise ValueError("Ollama tool call is missing its function object")
    name = function.get("name")
    arguments = function.get("arguments")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Ollama tool function name must be nonblank")
    if not isinstance(arguments, dict):
        raise ValueError("Ollama tool arguments must be an object, not encoded JSON")
    identifier = call.get("id")
    if "id" in call and (not isinstance(identifier, str) or not identifier.strip()):
        raise ValueError("Ollama optional tool call ID must be a nonblank string")
    return ToolCall(name, arguments, identifier)


def tool_message(call: ToolCall, result: dict[str, object]) -> dict[str, object]:
    message: dict[str, object] = {
        "role": "tool",
        "tool_name": call.name,
        "content": json.dumps(result, ensure_ascii=False, allow_nan=False),
    }
    if call.identifier is not None:
        message["tool_call_id"] = call.identifier
    return message

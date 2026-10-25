from __future__ import annotations

import json

import httpx

from .limits import CONNECT_TIMEOUT_SECONDS, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES


def serialize_request(payload: dict[str, object]) -> bytes:
    content = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(content) > MAX_REQUEST_BYTES:
        raise ValueError("Ollama serialized conversation/request exceeded 8 MiB")
    return content


async def read_json(
    client: httpx.AsyncClient, url: str, payload: dict[str, object] | None = None
) -> dict[str, object]:
    """Bound bodies during consumption; the process supervisor owns the deadline."""
    body = None if payload is None else serialize_request(payload)
    try:
        async with client.stream(
            "GET" if body is None else "POST",
            url,
            content=body,
            headers={} if body is None else {"Content-Type": "application/json"},
        ) as response:
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ValueError("Ollama server response exceeded 2 MiB")
                content.extend(chunk)
    except httpx.HTTPError as exc:
        raise ValueError(
            f"Ollama server request failed ({type(exc).__name__})"
        ) from exc
    try:
        data = json.loads(content, parse_constant=reject_nonfinite_json)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Ollama server returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("Ollama server returned a non-object JSON response")
    return data


def reject_nonfinite_json(value: str) -> object:
    raise ValueError(f"Nonfinite JSON number: {value}")


def discovery_client(timeout_seconds: float) -> httpx.AsyncClient:
    return http_client(httpx.Timeout(timeout_seconds))


def execution_client() -> httpx.AsyncClient:
    return http_client(httpx.Timeout(None, connect=CONNECT_TIMEOUT_SECONDS))


def http_client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=timeout)

from __future__ import annotations

import json

import httpx

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


async def read_json(
    client: httpx.AsyncClient, url: str, payload: dict[str, str] | None = None
) -> dict[str, object]:
    """Read bounded JSON; the discovery worker supervisor owns the total deadline."""
    try:
        async with client.stream(
            "GET" if payload is None else "POST", url, json=payload
        ) as response:
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ValueError("Ollama discovery response exceeded 2 MiB")
    except httpx.HTTPError as exc:
        raise ValueError(
            f"Ollama server request failed ({type(exc).__name__})"
        ) from exc
    try:
        data = json.loads(content)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Ollama server returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("Ollama server returned a non-object JSON response")
    return data


def discovery_client(timeout_seconds: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        trust_env=False, follow_redirects=False, timeout=timeout_seconds
    )

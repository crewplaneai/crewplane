from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .limits import DEFAULT_LIMITS, LogPresentationLimits

_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SENSITIVE_KEYS = {
    "token",
    "secret",
    "password",
    "authorization",
    "cookie",
    "api_key",
    "access_key",
    "secret_key",
    "private_key",
    "key",
}


def decode_sanitized(data: bytes) -> str:
    return sanitize_text(data.decode("utf-8", errors="replace"))


def sanitize_text(value: object) -> str:
    text = str(value)
    text = _ANSI_OSC_RE.sub("", text)
    text = _ANSI_CSI_RE.sub("", text)
    return _CONTROL_RE.sub("", text)


def clip_text(
    value: object,
    limit: int = DEFAULT_LIMITS.max_display_chars_per_record,
) -> str:
    text = sanitize_text(value)
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)]}..."


def sanitize_line(
    value: object,
    limits: LogPresentationLimits = DEFAULT_LIMITS,
) -> str:
    text = sanitize_text(value)
    line = " ".join(text.splitlines())
    return clip_text(line, limits.max_display_chars_per_record)


def sanitize_lines(
    values: list[str],
    limits: LogPresentationLimits = DEFAULT_LIMITS,
) -> tuple[str, ...]:
    return tuple(sanitize_line(value, limits) for value in values if value.strip())


def redact_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[redacted]" if is_sensitive_key(str(key)) else redact_json_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_json_value(item) for item in value]
    return value


def is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return (
        normalized in _SENSITIVE_KEYS
        or normalized.endswith("_key")
        or normalized.endswith("-key")
    )

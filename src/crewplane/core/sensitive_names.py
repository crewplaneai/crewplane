"""Name-based sensitivity policy shared by templates and configuration."""

from __future__ import annotations

import re

_SENSITIVE_NAME_PATTERN = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|credential|private)",
    re.IGNORECASE,
)


def is_sensitive_name(name: str) -> bool:
    return _SENSITIVE_NAME_PATTERN.search(name) is not None

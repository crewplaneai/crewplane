from __future__ import annotations

import re

QUOTA_PARSER_HINTS: dict[str, tuple[str, ...]] = {
    "codex": (
        "usage limit exceeded",
        "usage limit",
        "rate limit",
        "too many requests",
        "try again in",
        "retry after",
        "reset after",
        "reset at",
        "resetsat",
    ),
    "copilot": (
        "rate limit",
        "quota",
        "too many requests",
        "429",
        "retry after",
        "try again in",
    ),
    "claude": (
        "usage limit reached",
        "rate limit",
        "quota",
        "too many requests",
        "reset at",
        "retry after",
    ),
    "kilo": (
        "rate limit",
        "quota",
        "too many requests",
        "429",
        "retry after",
        "reset after",
        "try again in",
    ),
    "gemini": (
        "exhausted your capacity",
        "resource exhausted",
        "no capacity available",
        "retryable quota error",
        "max attempts reached",
        "rate limit exceeded",
        "too many requests",
        "429",
        "quota will reset",
        "quota exhausted",
        "retry after",
        "try again in",
    ),
    "generic": (),
}
QUOTA_CONTEXT_HINTS: tuple[str, ...] = (
    "quota",
    "rate limit",
    "usage limit",
    "capacity",
    "resource exhausted",
    "retry after",
    "retrying after",
    "try again",
    "reset",
    "429",
)
AMBIGUOUS_QUOTA_HINTS = frozenset(
    {
        "429",
        "capacity",
        "limit",
        "max attempts reached",
        "quota",
        "quota will reset",
        "rate limit",
        "reset after",
        "reset at",
        "retry after",
        "too many requests",
        "try again in",
        "usage limit",
    }
)
STRICT_QUOTA_EVIDENCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "too many requests",
        re.compile(
            r"^\s*(?:(?:error|request\s+failed)[:\s-]*)?"
            r"(?:you\s+(?:are|have\s+been)\s+)?(?:sending\s+)?"
            r"too\s+many\s+requests\b(?!\s+handling)|"
            r"\b(?:error|request\s+failed|status|code)\b[^\n.]{0,80}"
            r"\btoo\s+many\s+requests\b(?!\s+handling)|"
            r"\b429\b[^\n.]{0,80}\btoo\s+many\s+requests\b(?!\s+handling)|"
            r"\btoo\s+many\s+requests\b(?!\s+handling)[^\n.]{0,80}\b429\b",
            re.IGNORECASE,
        ),
    ),
    (
        "429",
        re.compile(
            r"^\s*(?:error|status|code|http(?:\s+status)?|response\s+status)"
            r"[:\s-]*429\b(?![^\n.]{0,40}\bhandling\b)|"
            r"\b(?:status|code|http\s+status|response\s+status)\b[^\n.]{0,20}"
            r"\b429\b(?![^\n.]{0,40}\bhandling\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "retryable quota error",
        re.compile(r"^\s*retryable\s*quota\s*error\b", re.IGNORECASE),
    ),
    (
        "resource exhausted",
        re.compile(
            r"^\s*resource[_\s-]+exhausted\b|"
            r"\b(?:status|code|error)\b[^\n]{0,40}\bresource[_\s-]+exhausted\b",
            re.IGNORECASE,
        ),
    ),
    (
        "exhausted your capacity",
        re.compile(
            r"\bexhausted\s+(?:your\s+)?capacity\b",
            re.IGNORECASE,
        ),
    ),
    (
        "no capacity available",
        re.compile(
            r"^\s*no\s+capacity\s+available\b|"
            r"\b(?:failed|error)[:\s-]+no\s+capacity\s+available\b",
            re.IGNORECASE,
        ),
    ),
    (
        "quota will reset",
        re.compile(r"\bquota\s+will\s+reset\s+(?:at|after)\b", re.IGNORECASE),
    ),
    (
        "limit reset",
        re.compile(
            r"\b(?:your\s+)?(?:usage\s+)?limit\s+will\s+reset\s+(?:at|after)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "quota reached",
        re.compile(
            r"\bquota\s+(?:reached|exceeded|exhausted|depleted)\b|"
            r"\bquota\b(?!\s+handling)[^\n.]{0,80}"
            r"\b(?:retry\s+after|try\s+again\s+in|reset\s+after)\s+\d",
            re.IGNORECASE,
        ),
    ),
    (
        "usage limit",
        re.compile(
            r"\b(?:you(?:'ve|\s+have)?\s+)?"
            r"(?:hit|reached|exceeded|exhausted)\s+your\s+usage\s+limit\b|"
            r"\busage\s+limit\s+(?:reached|exceeded|hit|exhausted)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "rate limit",
        re.compile(
            r"\brate\s+limit(?:ed)?\s+(?:reached|exceeded|hit)\b|"
            r"\brate\s+limited\b|"
            r"\brate\s+limit(?:ed)?\b(?!\s+handling)[^\n.]{0,80}"
            r"\b(?:retry\s+after|try\s+again\s+in|reset\s+after)\s+\d",
            re.IGNORECASE,
        ),
    ),
)
RELATIVE_RESET_PHRASES: tuple[str, ...] = (
    "quota will reset after",
    "will reset after",
    "reset after",
    "retrying after",
    "retry after",
    "please retry in",
    "try again in",
    "suggested retry after",
)
ABSOLUTE_RESET_PHRASES: tuple[str, ...] = (
    "reset at",
    "resets at",
    "retry at",
    "try again at",
)
DURATION_TOKEN_PATTERN = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>milliseconds?|ms|seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)"
    r"(?=[^a-zA-Z]|$)",
    re.IGNORECASE,
)
ISO_TIMESTAMP_PATTERN = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T"
    r"\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?\b"
)
MONTH_NAME_TIMESTAMP_PATTERN = re.compile(
    r"(?i)\b"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Sept(?:ember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\s+"
    r"\d{1,2}(?:st|nd|rd|th)?(?:,)?\s+"
    r"\d{4}\s+"
    r"\d{1,2}(?::\d{2})?\s*[ap]\.?(?:m)\.?\b"
)
EPOCH_PATTERN = re.compile(r"\b(?P<epoch>\d{10}(?:\.\d+)?|\d{13})\b")
PIPE_EPOCH_PATTERN = re.compile(r"\|\s*(?P<epoch>\d{10}(?:\.\d+)?|\d{13})\b")
LOCAL_TIME_WITH_TZ_PATTERN = re.compile(
    r"(?i)\b(?:reset(?:s)?|limit\s+will\s+reset|quota\s+will\s+reset|try\s+again|retry)\s+at\s+"
    r"(?P<time>\d{1,2}(?::\d{2})?\s*[ap]\.?(?:m)\.?)\s*"
    r"\((?P<tz>[A-Za-z_]+/[A-Za-z0-9_\-+]+)\)"
)
RESET_KEY_HINTS: tuple[str, ...] = (
    "resetsat",
    "resetat",
    "reset_at",
    "reset-at",
    "x-codex-primary-reset-at",
    "x-codex-secondary-reset-at",
)
DURATION_UNIT_MULTIPLIERS: dict[str, float] = {
    "ms": 0.001,
    "millisecond": 0.001,
    "milliseconds": 0.001,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 60.0 * 60.0,
    "hr": 60.0 * 60.0,
    "hrs": 60.0 * 60.0,
    "hour": 60.0 * 60.0,
    "hours": 60.0 * 60.0,
    "d": 60.0 * 60.0 * 24.0,
    "day": 60.0 * 60.0 * 24.0,
    "days": 60.0 * 60.0 * 24.0,
}

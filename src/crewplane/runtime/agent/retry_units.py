from __future__ import annotations

import re

RETRYING_AFTER_MS_PATTERN = re.compile(
    r"(?P<prefix>\bRetrying after\s+)(?P<milliseconds>\d+(?:\.\d+)?)ms\b",
    re.IGNORECASE,
)


def format_wait_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds:.3f}s"
    rounded = int(round(seconds))
    days, remainder = divmod(rounded, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return "".join(parts)


def format_stream_retry_wait(seconds: float) -> str:
    if seconds < 10:
        rounded = round(seconds, 1)
        if rounded.is_integer():
            return f"{int(rounded)}s"
        return f"{rounded:.1f}s"
    return format_wait_duration(seconds)


def normalize_retry_wait_units_in_text(text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        milliseconds_text = match.group("milliseconds")
        try:
            milliseconds = float(milliseconds_text)
        except ValueError:
            return match.group(0)
        seconds = max(milliseconds / 1000.0, 0.0)
        return f"{match.group('prefix')}{format_stream_retry_wait(seconds)}"

    return RETRYING_AFTER_MS_PATTERN.sub(_replace, text)

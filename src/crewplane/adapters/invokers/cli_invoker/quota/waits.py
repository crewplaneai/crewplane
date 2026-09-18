from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .lexicons import (
    ABSOLUTE_RESET_PHRASES,
    DURATION_TOKEN_PATTERN,
    DURATION_UNIT_MULTIPLIERS,
    EPOCH_PATTERN,
    ISO_TIMESTAMP_PATTERN,
    LOCAL_TIME_WITH_TZ_PATTERN,
    MONTH_NAME_TIMESTAMP_PATTERN,
    PIPE_EPOCH_PATTERN,
    RELATIVE_RESET_PHRASES,
    RESET_KEY_HINTS,
)

_MONTH_NAME_FORMATS = (
    "%b %d, %Y %I:%M %p",
    "%B %d, %Y %I:%M %p",
    "%b %d %Y %I:%M %p",
    "%B %d %Y %I:%M %p",
    "%b %d, %Y %I %p",
    "%B %d, %Y %I %p",
    "%b %d %Y %I %p",
    "%B %d %Y %I %p",
)
_CLOCK_TIME_FORMATS = ("%I:%M%p", "%I%p")


def extract_wait_candidates_from_line(line: str, now_utc: datetime) -> list[float]:
    """Collect nonnegative reset delays, retaining match order and duplicates."""
    candidates = _relative_waits(line)
    candidates.extend(_timezone_qualified_waits(line, now_utc))
    candidates.extend(_reset_key_waits(line, now_utc))
    candidates.extend(_pipe_epoch_waits(line, now_utc))
    candidates.extend(_absolute_phrase_waits(line, now_utc))
    return [candidate for candidate in candidates if candidate >= 0]


def _relative_waits(line: str) -> list[float]:
    candidates: list[float] = []
    lower_line = line.lower()
    for phrase in RELATIVE_RESET_PHRASES:
        start = 0
        while True:
            index = lower_line.find(phrase, start)
            if index == -1:
                break
            tail = line[index + len(phrase) :]
            duration_seconds = _parse_duration_seconds(tail)
            if duration_seconds is not None:
                candidates.append(duration_seconds)
            start = index + len(phrase)
    return candidates


def _duration_unit_to_seconds(unit: str) -> float | None:
    return DURATION_UNIT_MULTIPLIERS.get(unit.lower())


def _parse_duration_seconds(text: str) -> float | None:
    matches = list(DURATION_TOKEN_PATTERN.finditer(text))
    if not matches:
        return None

    total = 0.0
    recognised = False
    for match in matches:
        value = float(match.group("value"))
        multiplier = _duration_unit_to_seconds(match.group("unit"))
        if multiplier is None:
            continue
        recognised = True
        total += value * multiplier
    if not recognised:
        return None
    return max(total, 0.0)


def _timezone_qualified_waits(line: str, now_utc: datetime) -> list[float]:
    candidates: list[float] = []
    for match in LOCAL_TIME_WITH_TZ_PATTERN.finditer(line):
        local_wait = _parse_local_reset_wait_seconds(
            match.group("time"),
            match.group("tz"),
            now_utc,
        )
        if local_wait is not None:
            candidates.append(local_wait)
    return candidates


def _parse_local_reset_wait_seconds(
    local_time_text: str,
    timezone_name: str,
    now_utc: datetime,
) -> float | None:
    try:
        tz_info = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return None
    clock_time = _parse_clock_time(local_time_text)
    if clock_time is None:
        return None
    candidate_local = _next_local_reset(clock_time, now_utc.astimezone(tz_info))
    wait_seconds = (candidate_local.astimezone(UTC) - now_utc).total_seconds()
    if wait_seconds < 0:
        return None
    return wait_seconds


def _parse_clock_time(text: str) -> time | None:
    normalized = text.replace(" ", "").replace(".", "").upper()
    parsed = _parse_first_matching_datetime(normalized, _CLOCK_TIME_FORMATS)
    return None if parsed is None else parsed.time()


def _next_local_reset(clock_time: time, now_local: datetime) -> datetime:
    candidate_local = now_local.replace(
        hour=clock_time.hour,
        minute=clock_time.minute,
        second=0,
        microsecond=0,
    )
    if candidate_local <= now_local:
        candidate_local += timedelta(days=1)
    return candidate_local


def _reset_key_waits(line: str, now_utc: datetime) -> list[float]:
    lower_line = line.lower()
    if not any(key in lower_line for key in RESET_KEY_HINTS):
        return []
    candidates: list[float] = []
    for match in EPOCH_PATTERN.finditer(line):
        epoch_wait = _parse_epoch_wait_seconds(match.group("epoch"), now_utc)
        if epoch_wait is not None:
            candidates.append(epoch_wait)
    candidates.extend(_iso_waits(line, now_utc))
    return candidates


def _pipe_epoch_waits(line: str, now_utc: datetime) -> list[float]:
    lower_line = line.lower()
    if "|" not in line or not any(
        hint in lower_line for hint in ("reset", "quota", "limit")
    ):
        return []
    candidates: list[float] = []
    for match in PIPE_EPOCH_PATTERN.finditer(line):
        pipe_wait = _parse_epoch_wait_seconds(match.group("epoch"), now_utc)
        if pipe_wait is not None:
            candidates.append(pipe_wait)
    return candidates


def _normalize_epoch(epoch_value: float) -> float:
    if epoch_value >= 1_000_000_000_000:
        return epoch_value / 1000.0
    return epoch_value


def _parse_epoch_wait_seconds(epoch_text: str, now_utc: datetime) -> float | None:
    try:
        epoch_value = float(epoch_text)
    except ValueError:
        return None
    wait_seconds = _normalize_epoch(epoch_value) - now_utc.timestamp()
    if wait_seconds < 0:
        return None
    return wait_seconds


def _absolute_phrase_waits(line: str, now_utc: datetime) -> list[float]:
    lower_line = line.lower()
    if not any(phrase in lower_line for phrase in ABSOLUTE_RESET_PHRASES):
        return []
    candidates = _iso_waits(line, now_utc)
    for match in MONTH_NAME_TIMESTAMP_PATTERN.finditer(line):
        month_name_wait = _parse_month_name_wait_seconds(match.group(0), now_utc)
        if month_name_wait is not None:
            candidates.append(month_name_wait)
    return candidates


def _iso_waits(line: str, now_utc: datetime) -> list[float]:
    candidates: list[float] = []
    for match in ISO_TIMESTAMP_PATTERN.finditer(line):
        iso_wait = _parse_iso_wait_seconds(match.group(0), now_utc)
        if iso_wait is not None:
            candidates.append(iso_wait)
    return candidates


def _parse_iso_wait_seconds(iso_text: str, now_utc: datetime) -> float | None:
    normalized = iso_text.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    if re.search(r"[+-]\d{4}$", normalized):
        normalized = f"{normalized[:-2]}:{normalized[-2:]}"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    wait_seconds = (parsed.astimezone(UTC) - now_utc).total_seconds()
    if wait_seconds < 0:
        return None
    return wait_seconds


def _parse_month_name_wait_seconds(
    timestamp_text: str,
    now_utc: datetime,
) -> float | None:
    parsed_timestamp = _parse_month_name_timestamp(timestamp_text)
    if parsed_timestamp is None:
        return None
    local_timestamp = parsed_timestamp.replace(tzinfo=_local_timezone(now_utc))
    wait_seconds = (local_timestamp.astimezone(UTC) - now_utc).total_seconds()
    if wait_seconds < 0:
        return None
    return wait_seconds


def _parse_month_name_timestamp(text: str) -> datetime | None:
    normalized = re.sub(
        r"(?i)\b(\d{1,2})(?:st|nd|rd|th)\b",
        r"\1",
        text,
    )
    normalized = re.sub(r"\s+", " ", normalized.replace(".", "").strip())
    return _parse_first_matching_datetime(normalized, _MONTH_NAME_FORMATS)


def _local_timezone(now_utc: datetime) -> tzinfo:
    return now_utc.astimezone().tzinfo or UTC


def _parse_first_matching_datetime(
    text: str, formats: tuple[str, ...]
) -> datetime | None:
    for timestamp_format in formats:
        try:
            return datetime.strptime(text, timestamp_format)
        except ValueError:
            continue
    return None

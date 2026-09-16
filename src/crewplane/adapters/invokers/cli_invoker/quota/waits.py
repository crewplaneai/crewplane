from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, tzinfo
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


def _local_timezone(now_utc: datetime) -> tzinfo:
    return now_utc.astimezone().tzinfo or UTC


def _parse_month_name_wait_seconds(
    timestamp_text: str,
    now_utc: datetime,
) -> float | None:
    normalized = re.sub(
        r"(?i)\b(\d{1,2})(?:st|nd|rd|th)\b",
        r"\1",
        timestamp_text,
    )
    normalized = re.sub(r"\s+", " ", normalized.replace(".", "").strip())
    parsed_timestamp: datetime | None = None
    for fmt in (
        "%b %d, %Y %I:%M %p",
        "%B %d, %Y %I:%M %p",
        "%b %d %Y %I:%M %p",
        "%B %d %Y %I:%M %p",
        "%b %d, %Y %I %p",
        "%B %d, %Y %I %p",
        "%b %d %Y %I %p",
        "%B %d %Y %I %p",
    ):
        try:
            parsed_timestamp = datetime.strptime(normalized, fmt)
            break
        except ValueError:
            continue
    if parsed_timestamp is None:
        return None

    local_timestamp = parsed_timestamp.replace(tzinfo=_local_timezone(now_utc))
    wait_seconds = (local_timestamp.astimezone(UTC) - now_utc).total_seconds()
    if wait_seconds < 0:
        return None
    return wait_seconds


def _parse_local_reset_wait_seconds(
    local_time_text: str,
    timezone_name: str,
    now_utc: datetime,
) -> float | None:
    try:
        tz_info = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return None

    normalized_time = local_time_text.replace(" ", "").replace(".", "").upper()
    parsed_time: datetime | None = None
    for fmt in ("%I:%M%p", "%I%p"):
        try:
            parsed_time = datetime.strptime(normalized_time, fmt)
            break
        except ValueError:
            continue
    if parsed_time is None:
        return None

    now_local = now_utc.astimezone(tz_info)
    candidate_local = now_local.replace(
        hour=parsed_time.hour,
        minute=parsed_time.minute,
        second=0,
        microsecond=0,
    )
    if candidate_local <= now_local:
        candidate_local += timedelta(days=1)
    wait_seconds = (candidate_local.astimezone(UTC) - now_utc).total_seconds()
    if wait_seconds < 0:
        return None
    return wait_seconds


def extract_wait_candidates_from_line(line: str, now_utc: datetime) -> list[float]:
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

    for match in LOCAL_TIME_WITH_TZ_PATTERN.finditer(line):
        local_wait = _parse_local_reset_wait_seconds(
            match.group("time"),
            match.group("tz"),
            now_utc,
        )
        if local_wait is not None:
            candidates.append(local_wait)

    if any(key in lower_line for key in RESET_KEY_HINTS):
        for match in EPOCH_PATTERN.finditer(line):
            epoch_wait = _parse_epoch_wait_seconds(match.group("epoch"), now_utc)
            if epoch_wait is not None:
                candidates.append(epoch_wait)
        for match in ISO_TIMESTAMP_PATTERN.finditer(line):
            iso_wait = _parse_iso_wait_seconds(match.group(0), now_utc)
            if iso_wait is not None:
                candidates.append(iso_wait)

    if "|" in line and (
        "reset" in lower_line or "quota" in lower_line or "limit" in lower_line
    ):
        for match in PIPE_EPOCH_PATTERN.finditer(line):
            pipe_wait = _parse_epoch_wait_seconds(match.group("epoch"), now_utc)
            if pipe_wait is not None:
                candidates.append(pipe_wait)

    if any(phrase in lower_line for phrase in ABSOLUTE_RESET_PHRASES):
        for match in ISO_TIMESTAMP_PATTERN.finditer(line):
            iso_wait = _parse_iso_wait_seconds(match.group(0), now_utc)
            if iso_wait is not None:
                candidates.append(iso_wait)
        for match in MONTH_NAME_TIMESTAMP_PATTERN.finditer(line):
            month_name_wait = _parse_month_name_wait_seconds(match.group(0), now_utc)
            if month_name_wait is not None:
                candidates.append(month_name_wait)

    return [candidate for candidate in candidates if candidate >= 0]

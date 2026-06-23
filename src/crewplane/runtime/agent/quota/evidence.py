from __future__ import annotations

import re
from collections.abc import Iterable

from crewplane.core.config import AgentConfig

from .lexicons import (
    AMBIGUOUS_QUOTA_HINTS,
    QUOTA_CONTEXT_HINTS,
    QUOTA_PARSER_HINTS,
    STRICT_QUOTA_EVIDENCE_PATTERNS,
)


def _find_first_match(
    haystack: str, needles: list[str] | tuple[str, ...]
) -> str | None:
    if not haystack:
        return None
    haystack_lower = haystack.lower()
    haystack_collapsed = re.sub(r"[^a-z0-9]+", " ", haystack_lower).strip()
    haystack_squashed = re.sub(r"[^a-z0-9]+", "", haystack_lower)
    for needle in needles:
        if not needle:
            continue
        needle_lower = needle.lower()
        if needle_lower in haystack_lower:
            return needle
        needle_collapsed = re.sub(r"[^a-z0-9]+", " ", needle_lower).strip()
        if needle_collapsed and needle_collapsed in haystack_collapsed:
            return needle
        needle_squashed = re.sub(r"[^a-z0-9]+", "", needle_lower)
        if needle_squashed and needle_squashed in haystack_squashed:
            return needle
    return None


def _normalize_quota_hint(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _is_ambiguous_quota_hint(value: str) -> bool:
    return _normalize_quota_hint(value) in AMBIGUOUS_QUOTA_HINTS


def _find_strict_quota_evidence(line: str) -> str | None:
    for evidence, pattern in STRICT_QUOTA_EVIDENCE_PATTERNS:
        if pattern.search(line):
            return evidence
    return None


def _find_specific_quota_hint(
    line: str, hints: list[str] | tuple[str, ...]
) -> str | None:
    match = _find_first_match(line, hints)
    if match is None or _is_ambiguous_quota_hint(match):
        return None
    return match


def find_quota_evidence(
    output_lines: Iterable[str], parser: str, config: AgentConfig
) -> str | None:
    for line in output_lines:
        stripped = line.strip()
        if not stripped:
            continue
        strict_evidence = _find_strict_quota_evidence(stripped)
        if strict_evidence is not None:
            return strict_evidence
        config_match = _find_specific_quota_hint(
            stripped,
            config.quota_reached_on_contains,
        )
        if config_match is not None:
            return config_match
        parser_match = _find_specific_quota_hint(
            stripped,
            QUOTA_PARSER_HINTS.get(parser, ()),
        )
        if parser_match is not None:
            return parser_match
    return None


def _is_reset_context_line(line_lower: str, parser: str, config: AgentConfig) -> bool:
    if any(hint in line_lower for hint in QUOTA_CONTEXT_HINTS):
        return True
    if any(
        hint and hint.lower() in line_lower for hint in config.quota_reached_on_contains
    ):
        return True
    return any(hint in line_lower for hint in QUOTA_PARSER_HINTS.get(parser, ()))


def collect_quota_context_lines(
    output_lines: Iterable[str], parser: str, config: AgentConfig
) -> list[str]:
    context_lines: dict[int, str] = {}
    buffered_next_indices: set[int] = set()
    previous_line: str | None = None
    previous_index: int | None = None

    for index, raw_line in enumerate(output_lines):
        stripped = raw_line.strip()
        if index in buffered_next_indices:
            context_lines[index] = raw_line

        if not stripped:
            continue
        if not _is_reset_context_line(stripped.lower(), parser, config):
            continue

        if previous_index is not None and previous_line is not None:
            context_lines[previous_index] = previous_line
        context_lines[index] = raw_line
        buffered_next_indices.add(index + 1)

        previous_line = raw_line
        previous_index = index

    return [context_lines[index] for index in sorted(context_lines.keys())]

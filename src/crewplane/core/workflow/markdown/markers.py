from __future__ import annotations

import re
from pathlib import Path
from typing import assert_never, cast

from markdown_it import MarkdownIt

from crewplane.core.prompt_segments import PromptSegmentPayload, PromptSegmentRole

from .models import (
    ALLOWED_PROMPT_MARKER_ROLE_SET,
    ALLOWED_PROMPT_MARKER_ROLES,
    MarkerEvent,
    PromptMarkerRole,
)

CREWPLANE_OPEN_MARKER_PATTERN = re.compile(r"^<!--\s*crewplane:([a-z0-9._-]+)\s*-->$")
CREWPLANE_CLOSE_MARKER_PATTERN = re.compile(r"^<!--\s*/crewplane:([a-z0-9._-]+)\s*-->$")
CREWPLANE_MARKER_NAMESPACE_PATTERN = re.compile(
    r"^<!--\s*/?\s*crewplane\s*:",
    re.IGNORECASE,
)
COMMONMARK_PARSER = MarkdownIt("commonmark")


def validate_marker_role(raw_role: str, source: Path, node_id: str) -> PromptMarkerRole:
    if raw_role not in ALLOWED_PROMPT_MARKER_ROLE_SET:
        allowed_roles = ", ".join(ALLOWED_PROMPT_MARKER_ROLES)
        raise ValueError(
            f"{source} node '{node_id}' declares unknown crewplane role marker "
            f"'{raw_role}'. Allowed roles: {allowed_roles}."
        )
    return cast(PromptMarkerRole, raw_role)


def segment_role(active_role: PromptMarkerRole | None) -> PromptSegmentRole:
    return active_role or "shared"


def collect_marker_events(
    section_text: str,
    node_id: str,
    source: Path,
) -> list[MarkerEvent]:
    events: list[MarkerEvent] = []
    for token in COMMONMARK_PARSER.parse(section_text):
        if token.type != "html_block" or token.level != 0 or token.map is None:
            continue

        marker_text = token.content.strip()
        open_match = CREWPLANE_OPEN_MARKER_PATTERN.fullmatch(marker_text)
        if open_match is not None:
            role = validate_marker_role(open_match.group(1), source, node_id)
            events.append(
                MarkerEvent(
                    marker_kind="open",
                    role=role,
                    start_line=token.map[0],
                    end_line=token.map[1],
                )
            )
            continue

        close_match = CREWPLANE_CLOSE_MARKER_PATTERN.fullmatch(marker_text)
        if close_match is not None:
            role = validate_marker_role(close_match.group(1), source, node_id)
            events.append(
                MarkerEvent(
                    marker_kind="close",
                    role=role,
                    start_line=token.map[0],
                    end_line=token.map[1],
                )
            )
            continue

        if CREWPLANE_MARKER_NAMESPACE_PATTERN.match(marker_text) is not None:
            raise ValueError(
                f"{source} node '{node_id}' contains malformed crewplane marker "
                f"'{marker_text}'."
            )
    return events


def extract_prompt_segment_payloads(
    section_text: str,
    source: Path,
    node_id: str,
    content_start_line: int,
) -> tuple[list[PromptSegmentPayload], list[dict[str, int]]]:
    lines = section_text.splitlines(keepends=True)
    events = collect_marker_events(section_text, node_id=node_id, source=source)
    prompt_segments: list[PromptSegmentPayload] = []
    prompt_segment_spans: list[dict[str, int]] = []
    active_role: PromptMarkerRole | None = None
    cursor_line = 0

    for event in events:
        block_text = "".join(lines[cursor_line : event.start_line])
        if block_text:
            prompt_segments.append(
                {
                    "role": segment_role(active_role),
                    "content": block_text,
                }
            )
            prompt_segment_spans.append(
                {
                    "start_line": content_start_line + cursor_line,
                    "end_line": content_start_line + event.start_line,
                }
            )

        if event.marker_kind == "open":
            if active_role is not None:
                raise ValueError(
                    f"{source} node '{node_id}' contains nested crewplane role markers."
                )
            active_role = event.role
            cursor_line = event.end_line
            continue

        if event.marker_kind == "close":
            if active_role is None:
                raise ValueError(
                    f"{source} node '{node_id}' contains a crewplane role close marker "
                    "without a matching open marker."
                )
            if event.role != active_role:
                raise ValueError(
                    f"{source} node '{node_id}' closes role '{event.role}' while role "
                    f"'{active_role}' is open."
                )
            if not block_text.strip():
                raise ValueError(
                    f"{source} node '{node_id}' contains an empty '{active_role}' "
                    "crewplane role block."
                )

            active_role = None
            cursor_line = event.end_line
            continue

        assert_never(event.marker_kind)

    tail_text = "".join(lines[cursor_line:])
    if tail_text:
        prompt_segments.append(
            {
                "role": segment_role(active_role),
                "content": tail_text,
            }
        )
        prompt_segment_spans.append(
            {
                "start_line": content_start_line + cursor_line,
                "end_line": content_start_line + len(lines),
            }
        )
    if active_role is not None:
        raise ValueError(
            f"{source} node '{node_id}' has an unclosed '{active_role}' "
            "crewplane role marker."
        )
    return prompt_segments, prompt_segment_spans


def validate_input_node_section(
    section_text: str,
    source: Path,
    node_id: str,
) -> None:
    marker_events = collect_marker_events(section_text, node_id=node_id, source=source)
    if marker_events:
        raise ValueError(
            f"{source} node '{node_id}' is input mode and must not contain "
            "crewplane role markers."
        )
    if section_text.strip():
        raise ValueError(
            f"{source} node '{node_id}' is input mode and must not contain authored "
            "prompt text."
        )

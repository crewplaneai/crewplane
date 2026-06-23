from __future__ import annotations

from dataclasses import dataclass

# Read enough recent log content for live context without making refresh costly.
DEFAULT_MAX_TAIL_BYTES = 64 * 1024

# Skip single JSONL records that are too large to be useful in a compact pane.
DEFAULT_MAX_JSONL_RECORD_BYTES = 16 * 1024

# Claude-style JSON objects can contain full responses, so allow a larger parse cap.
DEFAULT_MAX_JSON_OBJECT_PARSE_BYTES = 2 * 1024 * 1024

# Keep one rendered provider event from dominating dashboard or inspect output.
DEFAULT_MAX_DISPLAY_CHARS_PER_RECORD = 1_000

# Bound repeated formatted inspect renders; raw inspect remains full-file.
DEFAULT_FORMATTED_INSPECT_LINE_BUDGET = 200

# Guard recursive provider payloads before JSON rendering walks them.
DEFAULT_MAX_JSON_DEPTH = 12

# The Crewplane-added provider log header is small and always at file start.
DEFAULT_HEADER_SCAN_BYTES = 4 * 1024

# Preserve enough mixed stdout/stderr diagnostics without flooding useful output.
DEFAULT_MAX_MIXED_STREAM_DIAGNOSTIC_LINES = 20

# Avoid repeatedly reparsing incomplete streaming JSON objects on every refresh.
DEFAULT_INCOMPLETE_JSON_OBJECT_MIN_PARSE_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class LogPresentationLimits:
    max_tail_bytes: int = DEFAULT_MAX_TAIL_BYTES
    max_jsonl_record_bytes: int = DEFAULT_MAX_JSONL_RECORD_BYTES
    max_json_object_parse_bytes: int = DEFAULT_MAX_JSON_OBJECT_PARSE_BYTES
    max_display_chars_per_record: int = DEFAULT_MAX_DISPLAY_CHARS_PER_RECORD
    max_json_depth: int = DEFAULT_MAX_JSON_DEPTH
    header_scan_bytes: int = DEFAULT_HEADER_SCAN_BYTES
    max_mixed_stream_diagnostic_lines: int = DEFAULT_MAX_MIXED_STREAM_DIAGNOSTIC_LINES
    incomplete_json_object_min_parse_interval_seconds: float = (
        DEFAULT_INCOMPLETE_JSON_OBJECT_MIN_PARSE_INTERVAL_SECONDS
    )


DEFAULT_LIMITS = LogPresentationLimits()

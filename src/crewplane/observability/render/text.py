from __future__ import annotations

from crewplane.observability.log_stream import MAX_STREAM_LINES_PER_NODE
from crewplane.observability.text_layout import fit_text, pad_text

ROW_HEIGHT = 3


def trim_to_height(lines: list[str], width: int, height: int) -> str:
    if len(lines) <= height:
        return "\n".join(fit_text(line, width) for line in lines)
    clipped = [fit_text(line, width) for line in lines[: height - 1]]
    clipped.append(fit_text("... output truncated to fit terminal height ...", width))
    return "\n".join(clipped)


def fit_cell(value: str, width: int) -> str:
    return pad_text(fit_text(value, width), width)


def clamped_stream_lines(value: int) -> int:
    return max(0, min(value, MAX_STREAM_LINES_PER_NODE))


def wave_row_height(stream_lines_per_node: int) -> int:
    return ROW_HEIGHT + clamped_stream_lines(stream_lines_per_node)

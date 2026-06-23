from __future__ import annotations

_ABOVE_OMISSION = "... above ..."
_BELOW_OMISSION = "... below ..."


def viewport_dag_lines(
    lines: list[str],
    height: int,
    selected_row: int | None,
) -> list[str]:
    if height <= 0 or not lines:
        return []
    if len(lines) <= height:
        return list(lines)

    preferred_row = _preferred_row_index(lines, selected_row)
    if height == 1:
        return [lines[preferred_row]]
    if height == 2:
        return _tiny_viewport(lines, preferred_row)
    return _standard_viewport(lines, height, preferred_row)


def _preferred_row_index(lines: list[str], selected_row: int | None) -> int:
    if selected_row is None or selected_row < 0 or selected_row >= len(lines):
        return 0
    return selected_row


def _tiny_viewport(lines: list[str], preferred_row: int) -> list[str]:
    hidden_above = preferred_row
    hidden_below = len(lines) - preferred_row - 1
    if hidden_above <= 0 and hidden_below <= 0:
        return [lines[preferred_row]]
    if hidden_above > 0 and hidden_below > 0:
        if hidden_below >= hidden_above:
            return [lines[preferred_row], _BELOW_OMISSION]
        return [_ABOVE_OMISSION, lines[preferred_row]]
    if hidden_above > 0:
        return [_ABOVE_OMISSION, lines[preferred_row]]
    return [lines[preferred_row], _BELOW_OMISSION]


def _standard_viewport(
    lines: list[str],
    height: int,
    preferred_row: int,
) -> list[str]:
    content_slots = max(1, height - 2)
    max_start = max(0, len(lines) - content_slots)
    start = min(max(0, preferred_row - content_slots // 2), max_start)
    end = min(len(lines), start + content_slots)

    while True:
        hidden_above = start > 0
        hidden_below = end < len(lines)
        used_rows = (end - start) + int(hidden_above) + int(hidden_below)
        extra_rows = height - used_rows
        if extra_rows <= 0:
            break
        if not hidden_above and end < len(lines):
            end += 1
            continue
        if not hidden_below and start > 0:
            start -= 1
            continue
        break

    hidden_above = start > 0
    hidden_below = end < len(lines)
    visible_lines = list(lines[start:end])
    if hidden_above:
        visible_lines.insert(0, _ABOVE_OMISSION)
    if hidden_below:
        visible_lines.append(_BELOW_OMISSION)
    return visible_lines

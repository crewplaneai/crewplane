from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...results.selection import parse_audit_round, parse_task_round
from .fields import int_field, nullable_int_field

INVALID_LINEAGE_ORDER = (-1, -1)


@dataclass(frozen=True)
class ReviewOutputCoordinates:
    task_id: str
    round_num: int
    audit_round_num: int | None


def review_output_coordinates(
    relative_path: str,
    expected_task_id: str,
) -> ReviewOutputCoordinates | None:
    """Parse and validate task and round coordinates from a review output path."""

    path = Path(relative_path)
    task_id, round_num = parse_task_round(path.stem)
    if task_id != expected_task_id or round_num <= 0:
        return None
    audit_round_num = None
    if len(path.parts) > 1:
        parsed_audit_round = parse_audit_round(path.parts[0])
        audit_round_num = parsed_audit_round if parsed_audit_round > 0 else None
    return ReviewOutputCoordinates(task_id, round_num, audit_round_num)


def invocation_round_order(payload: dict[str, object]) -> tuple[int, int] | None:
    """Return normalized audit/round coordinates, or None for invalid fields."""

    round_num = int_field(payload, "round_num")
    if round_num is None:
        return None
    audit_round_num = nullable_int_field(payload, "audit_round_num")
    if not audit_round_num.valid:
        return None
    return (audit_round_num.value or 0, round_num)

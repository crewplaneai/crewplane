"""Scalar defaults and coordinates shared by review execution and persistence."""

from typing import Final

DEFAULT_REMEDIATION_DEPTH: Final = 1
DEFAULT_AUDIT_ROUNDS: Final = 1


def remediation_depth_or_default(depth: int | None) -> int:
    return depth or DEFAULT_REMEDIATION_DEPTH


def audit_rounds_or_default(audit_rounds: int | None) -> int:
    return audit_rounds or DEFAULT_AUDIT_ROUNDS


def maximum_review_round(remediation_depth: int) -> int:
    """Include the initial review before the configured remediation opportunities."""
    return remediation_depth + 1


def audit_round_context(audit_rounds: int | None, audit_round_num: int) -> int | None:
    return audit_round_num if audit_rounds_or_default(audit_rounds) > 1 else None

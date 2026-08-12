from __future__ import annotations

from crewplane.core.workspace.policy import validate_worktree_name
from crewplane.version import SCHEMA_VERSION

from .keywords import (
    ALLOWED_NODE_MODE_SET,
    ALLOWED_NODE_MODES,
    ALLOWED_REVIEW_STARTS_WITH,
    ALLOWED_REVIEW_STARTS_WITH_SET,
    validate_exact_keyword,
)


def reject_removed_workspace_block(value: object, scope: str) -> object:
    """Reject the removed workspace block at either workflow schema boundary."""

    if not isinstance(value, dict) or "workspace" not in value:
        return value
    if scope == "node":
        raise ValueError(
            "node workspace blocks have been removed; use node worktree selectors"
        )
    raise ValueError(
        "workflow workspace blocks have been removed; use workflow worktrees"
    )


def normalize_node_mode(value: object) -> object:
    return validate_exact_keyword(
        value,
        field_name="node mode",
        allowed_values=ALLOWED_NODE_MODES,
        allowed_value_set=ALLOWED_NODE_MODE_SET,
    )


def normalize_review_starts_with(value: object) -> object:
    return validate_exact_keyword(
        value,
        field_name="review_starts_with",
        allowed_values=ALLOWED_REVIEW_STARTS_WITH,
        allowed_value_set=ALLOWED_REVIEW_STARTS_WITH_SET,
    )


def normalize_worktree_selector(value: object) -> object:
    if value is None or not isinstance(value, str):
        return value
    normalized = value.strip()
    if not normalized:
        raise ValueError("worktree selector cannot be blank")
    return normalized


def validate_workflow_schema_version(value: str) -> str:
    if value != SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported workflow schema version '{value}'. "
            f"Expected '{SCHEMA_VERSION}'."
        )
    return value


def normalize_worktree_declarations(value: object) -> object:
    if value is None:
        return {}
    if not isinstance(value, dict):
        return value
    normalized: dict[str, object] = {}
    for raw_name, declaration in value.items():
        if not isinstance(raw_name, str):
            raise ValueError("worktree names must be strings")
        name = validate_worktree_name(raw_name)
        if name in normalized:
            raise ValueError(f"Duplicate worktree name '{name}'")
        normalized[name] = declaration
    return normalized

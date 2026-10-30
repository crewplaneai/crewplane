"""Validate foreach node declarations before workflow model coercion."""

from __future__ import annotations

from typing import cast

from crewplane.core.workflow.collections import (
    collection_reference,
    validate_collection,
)
from crewplane.core.workflow.syntax import NODE_ID_PATTERN

FOREACH_FIELDS = frozenset(
    {
        "id",
        "mode",
        "needs",
        "workflow",
        "item_input",
        "items",
        "max_items",
        "inputs",
        "with",
        "with_params",
    }
)
FOREACH_ONLY_FIELDS = FOREACH_FIELDS - {"id", "mode", "needs"}


def validate_foreach_declaration(
    value: object, internal_parameters: bool = False
) -> object:
    """Validate a node declaration and normalize foreach bindings.

    Args:
        value: Raw node input. Non-dicts and non-foreach dicts without
            foreach-only fields pass through unchanged.
        internal_parameters: Allow the internal ``with_params`` spelling in
            addition to ``with``. Other field restrictions still apply.

    Returns:
        A shallow copy for foreach declarations, with ``item_input`` and binding
        keys stripped. Binding maps are copied and ``inputs`` node IDs are
        stripped; parameter values and other fields are unchanged, retaining
        references to items and dependencies. The input is never modified.

    Raises:
        ValueError: If a non-foreach node has foreach-only fields, or a foreach
            declaration has forbidden fields, invalid required strings, a
            non-fixed .md workflow path, a supplied max_items that is not a strict
            positive integer, an uncapped reference, invalid collection data, or invalid
            bindings. Checks run in field-policy, scalar, collection, then binding order;
            bindings are checked in ``inputs``, ``with``, ``with_params`` order.
    """
    if not isinstance(value, dict):
        return value
    if value.get("mode") != "foreach":
        _reject_foreach_only_fields(value)
        return value
    group = str(value.get("id", "<unknown>"))
    _validate_foreach_fields(value, group, internal_parameters)
    limit = _validate_foreach_scalars(value, group)
    _validate_foreach_items(value.get("items"), limit, group)
    return _normalize_foreach_declaration(value, group)


def _reject_foreach_only_fields(value: dict[str, object]) -> None:
    unexpected = FOREACH_ONLY_FIELDS.intersection(value)
    if unexpected:
        raise ValueError(
            f"Fields only valid for foreach: {', '.join(sorted(unexpected))}"
        )


def _validate_foreach_fields(
    value: dict[str, object], group: str, internal_parameters: bool
) -> None:
    forbidden = value.keys() - FOREACH_FIELDS
    if not internal_parameters and "with_params" in value:
        forbidden.add("with_params")
    if forbidden:
        raise ValueError(
            f"Foreach '{group}' cannot declare: {', '.join(sorted(forbidden))}"
        )


def _validate_foreach_scalars(value: dict[str, object], group: str) -> int | None:
    for field in ("workflow", "item_input"):
        text = value.get(field)
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Foreach '{group}' requires a non-empty {field}")
    workflow = cast(str, value["workflow"])
    if "{{" in workflow or not workflow.endswith(".md"):
        raise ValueError(f"Foreach '{group}' workflow must be a fixed .md path")
    if "max_items" not in value:
        return None
    limit = value["max_items"]
    if type(limit) is not int or limit <= 0:
        raise ValueError(
            f"Foreach '{group}' max_items must be a strict positive integer"
        )
    return limit


def _validate_foreach_items(items: object, limit: int | None, group: str) -> None:
    if isinstance(items, str):
        if limit is None:
            raise ValueError(f"Foreach '{group}' artifact reference requires max_items")
        collection_reference(items)
    else:
        if not isinstance(items, list):
            raise ValueError(f"Foreach '{group}' items: expected an array of objects")
        if limit is None:
            limit = len(items)
        validate_collection(items, limit, group)


def _normalize_foreach_declaration(
    value: dict[str, object], group: str
) -> dict[str, object]:
    result = dict(value)
    result["item_input"] = cast(str, value["item_input"]).strip()
    for field in ("inputs", "with", "with_params"):
        if field in result:
            result[field] = _bindings(result[field], group, field)
    return result


def _bindings(value: object, group: str, field: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"Foreach '{group}' {field} must be a string mapping")
    result: dict[str, str] = {}
    for key, binding in value.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(binding, str):
            raise ValueError(
                f"Foreach '{group}' {field} requires non-empty string keys and string values"
            )
        normalized = key.strip()
        if normalized in result:
            raise ValueError(
                f"Foreach '{group}' duplicate normalized {field} key '{normalized}'"
            )
        if field == "inputs" and not NODE_ID_PATTERN.fullmatch(binding.strip()):
            raise ValueError(
                f"Foreach '{group}' input '{key}' must reference a caller node ID"
            )
        result[normalized] = binding.strip() if field == "inputs" else binding
    return result

from __future__ import annotations

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
    if not isinstance(value, dict):
        return value
    if value.get("mode") != "foreach":
        unexpected = FOREACH_ONLY_FIELDS.intersection(value)
        if unexpected:
            raise ValueError(
                f"Fields only valid for foreach: {', '.join(sorted(unexpected))}"
            )
        return value
    group = str(value.get("id", "<unknown>"))
    forbidden = value.keys() - FOREACH_FIELDS
    if not internal_parameters and "with_params" in value:
        forbidden.add("with_params")
    if forbidden:
        raise ValueError(
            f"Foreach '{group}' cannot declare: {', '.join(sorted(forbidden))}"
        )
    for field in ("workflow", "item_input"):
        text = value.get(field)
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Foreach '{group}' requires a non-empty {field}")
    if "{{" in value["workflow"] or not value["workflow"].endswith(".md"):
        raise ValueError(f"Foreach '{group}' workflow must be a fixed .md path")
    limit = value.get("max_items")
    if type(limit) is not int or limit <= 0:
        raise ValueError(
            f"Foreach '{group}' max_items must be a strict positive integer"
        )
    items = value.get("items")
    if isinstance(items, str):
        collection_reference(items)
    else:
        validate_collection(items, limit, group)
    result = dict(value)
    result["item_input"] = value["item_input"].strip()
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

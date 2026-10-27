from pathlib import Path
from textwrap import indent

from pydantic import ValidationError
from pydantic_core import ErrorDetails
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from ..keywords import (
    ALLOWED_NODE_MODES,
    ALLOWED_PROVIDER_ROLES,
    ALLOWED_REVIEW_STARTS_WITH,
)

type FieldLocation = tuple[str | int, ...]


def format_frontmatter_errors(error: ValidationError, source: Path, root: Node) -> str:
    messages = []
    for detail in error.errors(include_context=False, include_url=False):
        location = _field_location(detail)
        if location is None:
            continue
        node = _source_node(root, location)
        # Frontmatter starts after the opening fence; YAML marks are zero-based.
        line = node.start_mark.line + 2
        column = node.start_mark.column + 1
        label = _location_label(root, location)
        message = indent(_error_message(detail, location), "    ")
        messages.append(f"{source}:{line}:{column}\n  {label}:\n{message}")
    return "\n\n".join(messages)


def _field_location(detail: ErrorDetails) -> FieldLocation | None:
    location = detail["loc"]
    match location:
        case ("nodes", int(), "providers", int(), "str"):
            return None if isinstance(detail["input"], dict) else location[:4]
        case ("nodes", int(), "providers", int(), "ProviderSpec", *fields):
            return location[:4] + tuple(fields) if fields else None
        case _:
            return location


def _source_node(root: Node, location: FieldLocation) -> Node:
    node = root
    for part in location:
        if isinstance(node, MappingNode):
            child = next(
                (value for key, value in node.value if key.value == part), None
            )
            if child is None:
                break
            node = child
        elif isinstance(node, SequenceNode) and isinstance(part, int):
            node = node.value[part]
        else:
            break
    return node


def _location_label(root: Node, location: FieldLocation) -> str:
    labels = []
    fields = location
    if len(fields) >= 2 and fields[0] == "nodes" and isinstance(fields[1], int):
        node_source = _source_node(root, fields[:2])
        node_id = _source_node(node_source, ("id",))
        labels.append(
            f"Node {node_id.value!r}"
            if isinstance(node_source, MappingNode) and isinstance(node_id, ScalarNode)
            else f"Node #{fields[1] + 1}"
        )
        fields = fields[2:]
        if len(fields) >= 2 and fields[0] == "providers" and isinstance(fields[1], int):
            provider = _source_node(root, location[:4] + ("provider",))
            label = f"provider #{fields[1] + 1}"
            if isinstance(provider, ScalarNode) and isinstance(
                _source_node(root, location[:4]), MappingNode
            ):
                label += f" ({provider.value!r})"
            labels.append(label)
            fields = fields[2:]
    field_path = "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}" for part in fields
    ).lstrip(".")
    if field_path:
        labels.append(field_path)
    return ", ".join(labels) or "frontmatter"


def _error_message(detail: ErrorDetails, location: FieldLocation) -> str:
    if detail["type"] == "missing":
        return "Field required"
    if detail["type"] == "model_type":
        return "Input should be a YAML object"
    allowed = _allowed_values(location)
    if allowed:
        value = detail["input"]
        display = (
            repr(value)
            if value is None or isinstance(value, (str, int, float, bool))
            else f"<{type(value).__name__}>"
        )
        message = f"Invalid value {display}.\nAllowed values: {', '.join(allowed)}."
        if isinstance(value, str) and value.lower() in allowed:
            message += "\nValues must be lower-case."
        return message
    if detail["loc"] != location and detail["loc"][-1] == "str":
        return "Expected a provider name (string) or a provider object."
    return detail["msg"].removeprefix("Value error, ")


def _allowed_values(location: FieldLocation) -> tuple[str, ...]:
    match location:
        case ("nodes", int(), "mode"):
            return ALLOWED_NODE_MODES
        case ("nodes", int(), "review_starts_with"):
            return ALLOWED_REVIEW_STARTS_WITH
        case ("nodes", int(), "providers", int(), "role"):
            return ALLOWED_PROVIDER_ROLES
        case _:
            return ()

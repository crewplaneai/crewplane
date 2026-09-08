from __future__ import annotations

from itertools import batched

BYTE_TRANSFORMING_ATTRIBUTES = frozenset(
    {"crlf", "eol", "filter", "ident", "text", "working-tree-encoding"}
)
ATTRIBUTE_CHECK_BATCH_SIZE = 100


def attribute_records(records: tuple[str, ...]) -> tuple[tuple[str, str, str], ...]:
    """Decode Git's NUL-separated path, attribute, and value records."""
    return tuple(
        (path, attribute.casefold(), value)
        for path, attribute, value in batched(records, 3, strict=True)
    )


def byte_transforming_attribute(attribute: str, value: str) -> bool:
    return attribute in BYTE_TRANSFORMING_ATTRIBUTES and value not in {
        "unset",
        "unspecified",
    }

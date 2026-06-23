from __future__ import annotations

BYTE_TRANSFORMING_ATTRIBUTES = frozenset(
    {
        "crlf",
        "eol",
        "filter",
        "ident",
        "text",
        "working-tree-encoding",
    }
)


def byte_transforming_attribute(attribute: str, value: str) -> bool:
    if attribute not in BYTE_TRANSFORMING_ATTRIBUTES:
        return False
    return value not in {"unset", "unspecified"}


def summarize_rejected_attributes(rejected: dict[str, list[str]]) -> str:
    return "; ".join(
        f"{attribute}={summarize_paths(paths)}"
        for attribute, paths in sorted(rejected.items())
    )


def summarize_paths(paths: list[str]) -> str:
    selected = paths[:5]
    suffix = (
        f" (+{len(paths) - len(selected)} more)" if len(paths) > len(selected) else ""
    )
    return ", ".join(selected) + suffix

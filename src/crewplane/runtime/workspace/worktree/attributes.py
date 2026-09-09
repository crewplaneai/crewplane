from __future__ import annotations


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

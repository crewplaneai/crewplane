from __future__ import annotations

from crewplane.core.workspace.git_policy import summarize_paths


def summarize_rejected_attributes(rejected: dict[str, list[str]]) -> str:
    return "; ".join(
        f"{attribute}={summarize_paths(paths)}"
        for attribute, paths in sorted(rejected.items())
    )

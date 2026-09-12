from __future__ import annotations

import subprocess
import tempfile
from itertools import batched
from pathlib import Path

from crewplane.core.workspace.git_attributes import (
    ATTRIBUTE_CHECK_BATCH_SIZE,
    attribute_records,
    byte_transforming_attribute,
)
from crewplane.core.workspace.git_policy import summarize_paths

from .git_source import (
    GitSourceContext,
    git_error,
    git_zero_records_with_env,
    run_git_with_env,
)
from .source_types import WorkspacePolicyBuilder


def validate_byte_transforming_attributes(
    project_root: Path,
    git_context: GitSourceContext,
    tracked_paths: list[str],
    builder: WorkspacePolicyBuilder,
) -> None:
    rejected: dict[str, list[str]] = {}
    with tempfile.TemporaryDirectory(prefix="crewplane-attr-index-") as temp_dir:
        env_overrides = {"GIT_INDEX_FILE": (Path(temp_dir) / "index").as_posix()}
        try:
            run_git_with_env(
                project_root,
                ("read-tree", git_context.run_base_commit),
                env_overrides,
            )
            collect_rejected_attributes(
                project_root,
                tracked_paths,
                env_overrides,
                rejected,
                builder,
            )
        except subprocess.CalledProcessError as exc:
            builder.errors.append(
                "Workspace source policy failed: Git attribute inspection failed "
                f"against the recorded source tree ({git_error(exc)})."
            )
            return
    if rejected:
        builder.errors.append(
            "Workspace source policy failed: workspace-enabled mode with "
            "worktree_contract: blob_exact does not support Git LFS or "
            "byte-transforming Git attributes: "
            f"{summarize_attributes(rejected)}. Remove those attributes for "
            "selected paths, commit already-normalized content without "
            "byte-transforming filters, or set settings.workspace.enabled: false "
            "for this run."
        )


def collect_rejected_attributes(
    project_root: Path,
    tracked_paths: list[str],
    env_overrides: dict[str, str],
    rejected: dict[str, list[str]],
    builder: WorkspacePolicyBuilder,
) -> None:
    for batch in batched(tracked_paths, ATTRIBUTE_CHECK_BATCH_SIZE, strict=False):
        records = git_zero_records_with_env(
            project_root,
            (
                "--literal-pathspecs",
                "check-attr",
                "--cached",
                "-z",
                "--all",
                "--",
                *batch,
            ),
            env_overrides,
        )
        if len(records) % 3 != 0:
            builder.errors.append(
                "Workspace source policy failed: Git check-attr returned an "
                "invalid record stream."
            )
            return
        for path, attribute, value in attribute_records(records):
            if byte_transforming_attribute(attribute, value):
                rejected.setdefault(attribute_description(attribute, value), []).append(
                    path
                )


def attribute_description(attribute: str, value: str) -> str:
    normalized_value = value.casefold()
    if attribute == "filter" and normalized_value == "lfs":
        return "Git LFS filter=lfs"
    if attribute == "filter":
        return f"custom Git filter={value}"
    if attribute in {"crlf", "eol", "text"}:
        return f"text normalization {attribute}={value}"
    if attribute == "working-tree-encoding":
        return f"working-tree-encoding={value}"
    if attribute == "ident":
        return f"ident expansion ident={value}"
    return f"{attribute}={value}"


def summarize_attributes(rejected: dict[str, list[str]]) -> str:
    parts = [
        f"{description}: {summarize_paths(paths)}"
        for description, paths in sorted(rejected.items())
    ]
    return "; ".join(parts)

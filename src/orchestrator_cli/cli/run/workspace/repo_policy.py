from __future__ import annotations

import unicodedata
from pathlib import Path

from orchestrator_cli.core.config import Settings
from orchestrator_cli.core.workspace_git_policy import (
    config_keys_from_scoped_records,
    effective_policy_lines,
    local_config_policy_summary,
)

from .git_attributes import validate_byte_transforming_attributes
from .git_source import (
    GitSourceContext,
    git_config_bool,
    git_config_value,
    git_path_exists,
    git_text,
    git_zero_records,
)
from .source_types import WorkspacePolicyBuilder

RESERVED_SOURCE_ROOTS = (
    ".orchestrator/execution-stages",
    ".orchestrator/execution-results",
    ".orchestrator/locks",
)
FULL_CHECKOUT_REMEDIATION = (
    "Use a full clone and full checkout before enabling workspace isolation, "
    "or set settings.workspace.enabled: false for this run."
)
STANDARD_REPO_REMEDIATION = (
    "Use a standard local repository state before enabling workspace isolation, "
    "or set settings.workspace.enabled: false for this run."
)
SUBMODULE_REMEDIATION = (
    "Set settings.workspace.enabled: false for this run, remove submodules for "
    "workspace-enabled runs, or restructure the workflow so managed worktree "
    "nodes do not require submodule materialization."
)


def validate_unsupported_repo_state(
    project_root: Path,
    git_context: GitSourceContext,
    builder: WorkspacePolicyBuilder,
) -> None:
    if git_text(project_root, "rev-parse", "--is-shallow-repository") == "true":
        builder.errors.append(
            unsupported_repo_error(
                "shallow repositories are unsupported.",
                FULL_CHECKOUT_REMEDIATION,
            )
        )
    if git_config_value(project_root, "extensions.partialclone"):
        builder.errors.append(
            unsupported_repo_error(
                "partial clone repositories are unsupported.",
                FULL_CHECKOUT_REMEDIATION,
            )
        )
    sparse_checkout_enabled = git_config_bool(project_root, "core.sparseCheckout")
    if sparse_checkout_enabled:
        builder.errors.append(
            unsupported_repo_error(
                "sparse checkout is unsupported.",
                FULL_CHECKOUT_REMEDIATION,
            )
        )
    for path_name, label in (
        ("objects/info/alternates", "object alternates"),
        ("info/grafts", "grafts"),
    ):
        path = git_context.common_git_dir / path_name
        if path.exists() and path.read_text(encoding="utf-8", errors="ignore").strip():
            builder.errors.append(
                unsupported_repo_error(
                    f"Git {label} are unsupported.",
                    STANDARD_REPO_REMEDIATION,
                )
            )
    if replacement_refs_exist(git_context.common_git_dir):
        builder.errors.append(
            unsupported_repo_error(
                "Git replacement refs are unsupported.",
                STANDARD_REPO_REMEDIATION,
            )
        )
    if tuple((git_context.common_git_dir / "objects" / "pack").glob("*.promisor")):
        builder.errors.append(
            unsupported_repo_error(
                "promisor object packs are unsupported.",
                FULL_CHECKOUT_REMEDIATION,
            )
        )
    sparse_checkout = git_context.active_git_dir / "info" / "sparse-checkout"
    if (
        sparse_checkout_enabled
        and sparse_checkout.exists()
        and sparse_checkout.read_text(encoding="utf-8", errors="ignore").strip()
    ):
        builder.errors.append(
            unsupported_repo_error(
                "sparse checkout state is unsupported.",
                FULL_CHECKOUT_REMEDIATION,
            )
        )
    merge_head = git_context.active_git_dir / "MERGE_HEAD"
    if (
        merge_head.exists()
        and merge_head.read_text(encoding="utf-8", errors="ignore").strip()
    ):
        builder.errors.append(
            unsupported_repo_error(
                "in-progress merge state is unsupported.",
                "Finish or abort the merge before enabling workspace isolation, "
                "or set settings.workspace.enabled: false for this run.",
            )
        )


def validate_local_git_config(
    project_root: Path,
    builder: WorkspacePolicyBuilder,
) -> dict[str, tuple[str, ...]]:
    policy_summary = inspect_local_git_config(project_root)
    rejected = policy_summary["rejected"]
    if rejected:
        builder.errors.append(
            "Workspace source policy failed: local Git config contains unsupported "
            f"keys: {', '.join(rejected)}. Workspace-enabled mode with "
            "worktree_contract: blob_exact fails closed before provider "
            f"invocation. {STANDARD_REPO_REMEDIATION}"
        )
    return policy_summary


def inspect_local_git_config(project_root: Path) -> dict[str, tuple[str, ...]]:
    records = git_zero_records(
        project_root,
        "config",
        "--local",
        "--no-includes",
        "--list",
        "--name-only",
        "--show-origin",
        "--show-scope",
        "-z",
    )
    config_keys = config_keys_from_scoped_records(records)
    return local_config_policy_summary(config_keys)


def validate_local_policy_files(
    git_context: GitSourceContext,
    builder: WorkspacePolicyBuilder,
) -> None:
    attribute_paths = [
        path
        for path in local_git_info_paths(git_context, "attributes")
        if path.exists()
    ]
    non_empty_attributes = [
        path
        for path in attribute_paths
        if path.read_text(encoding="utf-8", errors="ignore").strip()
    ]
    if non_empty_attributes:
        builder.errors.append(
            "Workspace source policy failed: Git info/attributes must be empty. "
            "Workspace-enabled mode with worktree_contract: blob_exact does not "
            "support local attribute overrides. Remove the local override or set "
            "settings.workspace.enabled: false for this run. Files: "
            f"{summarize_policy_paths(non_empty_attributes)}."
        )
    exclude_paths = [
        path for path in local_git_info_paths(git_context, "exclude") if path.exists()
    ]
    effective_excludes = [
        path for path in exclude_paths if effective_policy_lines(path)
    ]
    if effective_excludes:
        builder.errors.append(
            "Workspace source policy failed: Git info/exclude contains effective "
            "patterns, which are unsupported by blob_exact workspaces. Move the "
            "ignore rules into committed project policy when needed, or set "
            "settings.workspace.enabled: false for this run. Files: "
            f"{summarize_policy_paths(effective_excludes)}."
        )
    worktree_config = git_context.active_git_dir / "config.worktree"
    if worktree_config.exists() and worktree_config.stat().st_size > 0:
        builder.errors.append(
            "Workspace source policy failed: worktree-specific Git config is "
            "unsupported by blob_exact workspaces. Remove the worktree-specific "
            "config before enabling workspace isolation, or set "
            "settings.workspace.enabled: false for this run."
        )


def local_git_info_paths(
    git_context: GitSourceContext,
    filename: str,
) -> tuple[Path, ...]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for git_dir in (git_context.active_git_dir, git_context.common_git_dir):
        path = git_dir / "info" / filename
        normalized = path.resolve(strict=False)
        if normalized in seen:
            continue
        seen.add(normalized)
        paths.append(path)
    return tuple(paths)


def validate_index_flags(
    project_root: Path,
    builder: WorkspacePolicyBuilder,
) -> None:
    flagged_paths: list[str] = []
    for record in git_zero_records(project_root, "ls-files", "-v", "-z"):
        if len(record) < 3:
            continue
        flag = record[0]
        path = record[2:]
        if flag == "S" or flag.islower():
            flagged_paths.append(path)
    if flagged_paths:
        builder.errors.append(
            "Workspace source policy failed: index contains skip-worktree or "
            f"assume-unchanged entries: {summarize_paths(flagged_paths)}. Clear "
            "those index flags before enabling workspace isolation, or set "
            "settings.workspace.enabled: false for this run."
        )


def validate_clean_start(
    project_root: Path,
    settings: Settings,
    builder: WorkspacePolicyBuilder,
    logical_worktree_names: tuple[str, ...] = (),
    git_context: GitSourceContext | None = None,
) -> None:
    clean_start = run_clean_start(settings)
    worktree_context = worktree_name_context(logical_worktree_names)
    tracked: list[str] = []
    untracked: list[str] = []
    for record in git_zero_records(
        project_root,
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
    ):
        if record.startswith(("1 ", "2 ", "u ")):
            tracked.append(status_path(record))
        elif record.startswith("? "):
            path = record[2:]
            project_path = project_root_relative_source_path(path, git_context)
            if project_path is not None and not is_reserved_source_path(project_path):
                untracked.append(path)
    if tracked:
        builder.errors.append(
            "Workspace source policy failed: tracked files have staged or "
            f"unstaged changes: {summarize_paths(tracked)}.{worktree_context} "
            "Commit or stash source changes, or run from a clean checkout."
        )
    if clean_start == "strict" and untracked:
        builder.errors.append(
            "Workspace source policy failed: strict clean_start rejects untracked "
            f"source files: {summarize_paths(untracked)}.{worktree_context} "
            "Commit required files or use clean_start: tracked_only."
        )
    if clean_start == "tracked_only" and untracked:
        builder.warnings.append(
            "Workspace source policy warning: tracked_only excluded "
            f"{len(untracked)} untracked files. Excluded files are not visible to "
            f"providers.{worktree_context} Commit required files or use explicit "
            "allowlisted external resources. Examples: "
            f"{summarize_paths(untracked)}."
        )


def validate_source_tree(
    git_context: GitSourceContext,
    builder: WorkspacePolicyBuilder,
) -> None:
    tree_records = git_zero_records(
        git_context.git_top_level,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        "--full-name",
        git_context.source_tree,
    )
    gitlinks: list[str] = []
    tracked_paths: list[str] = []
    collision_paths: dict[str, str] = {}
    for record in tree_records:
        header, _, path = record.partition("\t")
        mode = header.split(" ", 1)[0]
        tracked_paths.append(path)
        if mode == "160000":
            gitlinks.append(path)
        project_path = project_root_relative_source_path(path, git_context)
        if project_path is not None and is_reserved_source_path(project_path):
            builder.errors.append(
                "Workspace source policy failed: tracked files under reserved "
                f"runtime artifact roots are unsupported: {path}. Move tracked "
                "source files out of .orchestrator runtime artifact directories "
                "or set settings.workspace.enabled: false for this run."
            )
        folded_path = path_collision_key(path)
        existing_path = collision_paths.setdefault(folded_path, path)
        if existing_path != path:
            builder.errors.append(
                "Workspace source policy failed: tracked paths collide under "
                f"case or Unicode normalization: {existing_path}, {path}."
            )
    if gitlinks:
        builder.errors.append(
            "Workspace source policy failed: submodules/gitlinks are unsupported: "
            f"{summarize_paths(gitlinks)}. {SUBMODULE_REMEDIATION}"
        )
    if git_path_exists(
        git_context.git_top_level,
        f"{git_context.run_base_commit}:.gitmodules",
    ):
        builder.errors.append(
            "Workspace source policy failed: repositories with .gitmodules are "
            f"unsupported by workspace-enabled mode. {SUBMODULE_REMEDIATION}"
        )
    validate_byte_transforming_attributes(
        git_context.git_top_level,
        git_context,
        tracked_paths,
        builder,
    )


def path_collision_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def unsupported_repo_error(summary: str, remediation: str) -> str:
    return (
        f"Workspace source policy failed: {summary} Workspace-enabled mode with "
        "worktree_contract: blob_exact fails closed before provider invocation. "
        f"{remediation}"
    )


def run_clean_start(settings: Settings) -> str:
    return settings.workspace.clean_start


def replacement_refs_exist(common_git_dir: Path) -> bool:
    replace_root = common_git_dir / "refs" / "replace"
    if replace_root.exists() and any(
        path.is_file() for path in replace_root.rglob("*")
    ):
        return True
    packed_refs = common_git_dir / "packed-refs"
    if not packed_refs.exists():
        return False
    return any(
        " refs/replace/" in line
        for line in packed_refs.read_text(
            encoding="utf-8", errors="ignore"
        ).splitlines()
        if line and not line.startswith(("#", "^"))
    )


def status_path(record: str) -> str:
    parts = record.split(" ")
    return parts[-1] if parts else record


def is_reserved_source_path(path: str) -> bool:
    return any(
        path == root or path.startswith(f"{root}/") for root in RESERVED_SOURCE_ROOTS
    )


def project_root_relative_source_path(
    path: str,
    git_context: GitSourceContext | None,
) -> str | None:
    if git_context is None or git_context.project_root_relative_path == ".":
        return path
    prefix = f"{git_context.project_root_relative_path}/"
    if path == git_context.project_root_relative_path:
        return "."
    if path.startswith(prefix):
        return path.removeprefix(prefix)
    return None


def summarize_paths(paths: list[str]) -> str:
    selected = paths[:5]
    suffix = (
        f" (+{len(paths) - len(selected)} more)" if len(paths) > len(selected) else ""
    )
    return ", ".join(selected) + suffix


def summarize_policy_paths(paths: list[Path]) -> str:
    return summarize_paths([path.as_posix() for path in paths])


def worktree_name_context(logical_worktree_names: tuple[str, ...]) -> str:
    if not logical_worktree_names:
        return ""
    return (
        " Required by logical worktrees: "
        f"{summarize_paths(list(logical_worktree_names))}."
    )

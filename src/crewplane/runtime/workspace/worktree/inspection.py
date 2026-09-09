from __future__ import annotations

import subprocess
from dataclasses import dataclass
from itertools import batched
from pathlib import Path

from crewplane.core.state_paths import RUNTIME_ARTIFACT_ROOTS, is_reserved_state_path
from crewplane.core.workspace.git_attributes import (
    ATTRIBUTE_CHECK_BATCH_SIZE,
    attribute_records,
    byte_transforming_attribute,
)

from ..git import git, git_error
from .attributes import summarize_rejected_attributes
from .policy import (
    reject_common_git_policy_drift,
    reject_worktree_git_policy_drift,
)
from .protected_refs import ProtectedRefSnapshot, reject_protected_ref_drift


@dataclass(frozen=True)
class WorktreeDriftSummary:
    final_head: str | None
    changed_path_count: int
    diagnostics: tuple[dict[str, str], ...]


def changed_paths(checkout_root: Path) -> tuple[str, ...]:
    records = git(checkout_root).zero_records(
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    paths: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        if len(record) < 4:
            index += 1
            continue
        status = record[:2]
        paths.append(record[3:])
        if status.startswith(("R", "C")):
            index += 1
            if index < len(records):
                paths.append(records[index])
        index += 1
    return tuple(paths)


def changed_tree_paths(
    checkout_root: Path,
    source_tree: str,
    result_tree: str,
) -> tuple[str, ...]:
    return git(checkout_root).zero_records(
        "diff-tree",
        "-r",
        "--no-commit-id",
        "--name-only",
        "-z",
        source_tree,
        result_tree,
    )


def reject_gitattributes_drift(
    checkout_root: Path,
    source_commit: str,
    paths: tuple[str, ...],
) -> None:
    if gitattributes_drift_detected(paths) or filesystem_gitattributes_drift_detected(
        checkout_root,
        source_commit,
    ):
        raise RuntimeError(
            "Workspace providers may not create or modify .gitattributes."
        )


def reject_gitignore_drift(
    checkout_root: Path,
    source_commit: str,
) -> None:
    if filesystem_gitignore_drift_detected(checkout_root, source_commit):
        raise RuntimeError(
            "Workspace providers may not create, modify, or delete .gitignore."
        )


def reject_byte_transforming_attributes(
    checkout_root: Path,
    paths: tuple[str, ...],
) -> None:
    rejected: dict[str, list[str]] = {}
    command = git(checkout_root)
    for batch in batched(paths, ATTRIBUTE_CHECK_BATCH_SIZE, strict=False):
        records = command.zero_records(
            "--literal-pathspecs",
            "check-attr",
            "-z",
            "--all",
            "--",
            *batch,
        )
        if len(records) % 3 != 0:
            raise RuntimeError("Git returned an invalid attribute record stream.")
        for path, attribute, value in attribute_records(records):
            if byte_transforming_attribute(attribute, value):
                rejected.setdefault(attribute, []).append(path)
    if rejected:
        raise RuntimeError(
            "Workspace result capture rejected byte-transforming Git attributes: "
            f"{summarize_rejected_attributes(rejected)}."
        )


def inspect_disposable_checkout(
    checkout_root: Path,
    source_commit: str,
    protected_refs: ProtectedRefSnapshot,
    repo_root: Path,
    common_git_dir: Path,
    project_root_relative_path: str = ".",
) -> WorktreeDriftSummary:
    diagnostics: list[dict[str, str]] = []
    final_head = _final_head_or_diagnostic(checkout_root, diagnostics)
    if final_head is not None and final_head != source_commit:
        diagnostics.append(
            {
                "level": "warning",
                "message": (
                    "Disposable workspace provider moved HEAD; reviewer/remediation "
                    "workspace drift was discarded."
                ),
            }
        )
    reject_common_git_policy_drift(repo_root, common_git_dir)
    reject_worktree_git_policy_drift(checkout_root)
    reject_protected_ref_drift(checkout_root, protected_refs)
    paths = _changed_paths_or_diagnostic(checkout_root, diagnostics)
    if paths:
        diagnostics.append(
            {
                "level": "warning",
                "message": (
                    f"Disposable workspace provider changed {len(paths)} path(s); "
                    "changes were discarded."
                ),
            }
        )
    if gitattributes_drift_detected(paths) or _filesystem_gitattributes_diagnostic(
        checkout_root,
        source_commit,
        diagnostics,
    ):
        diagnostics.append(
            {
                "level": "warning",
                "message": (
                    "Disposable workspace provider changed .gitattributes; drift "
                    "was discarded."
                ),
            }
        )
    if any(reserved_runtime_path(path, project_root_relative_path) for path in paths):
        diagnostics.append(
            {
                "level": "warning",
                "message": (
                    "Disposable workspace provider changed reserved runtime paths; "
                    "drift was discarded."
                ),
            }
        )
    return WorktreeDriftSummary(
        final_head=final_head,
        changed_path_count=len(paths),
        diagnostics=tuple(diagnostics),
    )


def reserved_runtime_path(path: str, project_root_relative_path: str = ".") -> bool:
    if is_reserved_state_path(path, RUNTIME_ARTIFACT_ROOTS):
        return True
    project_root = project_root_relative_path.strip("/")
    if project_root in {"", "."}:
        return False
    prefix = f"{project_root}/"
    if not path.startswith(prefix):
        return False
    return is_reserved_state_path(path.removeprefix(prefix), RUNTIME_ARTIFACT_ROOTS)


def gitattributes_drift_detected(paths: tuple[str, ...]) -> bool:
    return any(Path(path).name == ".gitattributes" for path in paths)


def filesystem_gitattributes_drift_detected(
    checkout_root: Path,
    source_commit: str,
) -> bool:
    return _filesystem_policy_file_drift_detected(
        checkout_root,
        source_commit,
        ".gitattributes",
    )


def filesystem_gitignore_drift_detected(
    checkout_root: Path,
    source_commit: str,
) -> bool:
    return _filesystem_policy_file_drift_detected(
        checkout_root,
        source_commit,
        ".gitignore",
    )


def _filesystem_policy_file_drift_detected(
    checkout_root: Path,
    source_commit: str,
    file_name: str,
) -> bool:
    baseline = _policy_file_blob_ids(checkout_root, source_commit, file_name)
    current_paths = _filesystem_policy_file_paths(checkout_root, file_name)
    if file_name == ".gitignore":
        ignored_parents = _ignored_gitignore_parents(
            checkout_root, set(current_paths) - baseline.keys()
        )
        current_paths = tuple(
            path
            for path in current_paths
            if path in baseline or Path(path).parent.as_posix() not in ignored_parents
        )
    if set(current_paths) != set(baseline):
        return True
    command = git(checkout_root)
    for path in current_paths:
        filesystem_path = checkout_root / path
        if filesystem_path.is_symlink() or not filesystem_path.is_file():
            return True
        object_id = command.text("hash-object", "--no-filters", "--", path)
        if object_id != baseline[path]:
            return True
    return False


def _ignored_gitignore_parents(checkout_root: Path, paths: set[str]) -> set[str]:
    parents = sorted({Path(path).parent.as_posix() for path in paths} - {"."})
    if not parents:
        return set()
    # A trailing slash lets a directory's own .gitignore match the query.
    parent_records = "\0".join(parents) + "\0"
    try:
        result = git(checkout_root).run_with_input(
            parent_records.encode("utf-8"),
            "check-ignore",
            "--no-index",
            "-z",
            "--stdin",
        )
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return set()
        raise
    return set(result.stdout.decode("utf-8").split("\0")) - {""}


def _final_head_or_diagnostic(
    checkout_root: Path,
    diagnostics: list[dict[str, str]],
) -> str | None:
    try:
        return git(checkout_root).text("rev-parse", "HEAD^{commit}")
    except subprocess.CalledProcessError as exc:
        diagnostics.append(
            {
                "level": "warning",
                "message": (
                    "Disposable workspace final HEAD could not be inspected: "
                    f"{git_error(exc)}"
                ),
            }
        )
    return None


def _append_policy_diagnostics(
    checkout_root: Path,
    repo_root: Path,
    common_git_dir: Path,
    diagnostics: list[dict[str, str]],
) -> None:
    try:
        reject_common_git_policy_drift(repo_root, common_git_dir)
        reject_worktree_git_policy_drift(checkout_root)
    except RuntimeError as exc:
        diagnostics.append(
            {
                "level": "warning",
                "message": f"Disposable workspace Git policy drift: {exc}",
            }
        )


def _append_protected_ref_diagnostics(
    checkout_root: Path,
    protected_refs: ProtectedRefSnapshot,
    diagnostics: list[dict[str, str]],
) -> None:
    try:
        reject_protected_ref_drift(checkout_root, protected_refs)
    except RuntimeError as exc:
        diagnostics.append(
            {
                "level": "warning",
                "message": f"Disposable workspace protected ref drift: {exc}",
            }
        )


def _changed_paths_or_diagnostic(
    checkout_root: Path,
    diagnostics: list[dict[str, str]],
) -> tuple[str, ...]:
    try:
        return changed_paths(checkout_root)
    except subprocess.CalledProcessError as exc:
        diagnostics.append(
            {
                "level": "warning",
                "message": (
                    "Disposable workspace changed paths could not be inspected: "
                    f"{git_error(exc)}"
                ),
            }
        )
    return ()


def _filesystem_gitattributes_diagnostic(
    checkout_root: Path,
    source_commit: str,
    diagnostics: list[dict[str, str]],
) -> bool:
    try:
        return filesystem_gitattributes_drift_detected(checkout_root, source_commit)
    except subprocess.CalledProcessError as exc:
        diagnostics.append(
            {
                "level": "warning",
                "message": (
                    "Disposable workspace .gitattributes drift could not be "
                    f"inspected: {git_error(exc)}"
                ),
            }
        )
    return False


def _policy_file_blob_ids(
    checkout_root: Path,
    source_commit: str,
    file_name: str,
) -> dict[str, str]:
    records = git(checkout_root).zero_records(
        "ls-tree",
        "-r",
        "-z",
        source_commit,
    )
    blob_ids: dict[str, str] = {}
    for record in records:
        header, separator, path = record.partition("\t")
        if separator != "\t" or Path(path).name != file_name:
            continue
        parts = header.split(" ")
        if len(parts) != 3:
            raise RuntimeError(f"Git returned an invalid {file_name} tree entry.")
        blob_ids[path] = parts[2]
    return blob_ids


def _filesystem_policy_file_paths(
    checkout_root: Path,
    file_name: str,
) -> tuple[str, ...]:
    paths: list[str] = []
    for current_root, dir_names, file_names in checkout_root.walk(
        follow_symlinks=False,
    ):
        dir_names[:] = [name for name in dir_names if name != ".git"]
        if file_name in file_names:
            paths.append(
                (current_root / file_name).relative_to(checkout_root).as_posix()
            )
    return tuple(sorted(paths))

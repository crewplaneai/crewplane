from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from crewplane.core.workspace.git_policy import (
    SUPPORTED_RESULT_TREE_MODES,
    portable_path_key,
)

from ..git import GitCommand, git
from .inspection import reserved_runtime_path


@dataclass(frozen=True, slots=True)
class _ResultTreeEntry:
    """Represent one parsed entry from a Git tree listing."""

    mode: str
    object_type: str
    object_id: str
    path: str


def validate_result_tree(
    checkout_root: Path,
    tree: str,
    project_root_relative_path: str = ".",
) -> None:
    """Validate that a Git tree is safe to publish as a workspace result.

    Raises:
        RuntimeError: If the tree contains unsafe entries or invalid Git objects.
    """

    command = git(checkout_root)
    entries = tuple(
        _validated_result_tree_entry(record, project_root_relative_path)
        for record in command.zero_records(
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            tree,
        )
    )
    _validate_result_blobs(command, [entry.object_id for entry in entries])
    validate_portable_path_collisions(entry.path for entry in entries)


def _validated_result_tree_entry(
    record: str,
    project_root_relative_path: str,
) -> _ResultTreeEntry:
    entry = _parse_result_tree_entry(record)
    _validate_result_tree_entry(entry, project_root_relative_path)
    return entry


def _parse_result_tree_entry(record: str) -> _ResultTreeEntry:
    header, separator, path = record.partition("\t")
    if separator != "\t":
        raise RuntimeError("Workspace result tree contains an invalid entry.")
    fields = header.split()
    if len(fields) != 3:
        raise RuntimeError("Workspace result tree contains invalid object metadata.")
    mode, object_type, object_id = fields
    return _ResultTreeEntry(
        mode=mode,
        object_type=object_type,
        object_id=object_id,
        path=path,
    )


def _validate_result_tree_entry(
    entry: _ResultTreeEntry,
    project_root_relative_path: str,
) -> None:
    if entry.mode not in SUPPORTED_RESULT_TREE_MODES:
        raise RuntimeError(
            f"Workspace result tree contains unsupported mode {entry.mode}."
        )
    if entry.object_type != "blob":
        raise RuntimeError(
            "Workspace result tree contains unsupported object type "
            f"{entry.object_type}."
        )
    _validate_result_path(entry.path)
    if reserved_runtime_path(entry.path, project_root_relative_path):
        raise RuntimeError(
            "Workspace result tree contains reserved runtime artifact paths."
        )


def _validate_result_blobs(command: GitCommand, object_ids: list[str]) -> None:
    if not object_ids:
        return
    output = command.run_with_input(
        "".join(f"{object_id}\n" for object_id in object_ids).encode(),
        "cat-file",
        "--batch-check=%(objectname) %(objecttype)",
    ).stdout.decode("utf-8", errors="strict")
    records = output.splitlines()
    if len(records) != len(object_ids):
        raise RuntimeError("Workspace result tree object verification was incomplete.")
    for expected_oid, record in zip(object_ids, records, strict=True):
        actual_oid, separator, object_type = record.partition(" ")
        if separator != " " or actual_oid != expected_oid or object_type != "blob":
            raise RuntimeError(
                "Workspace result tree contains a missing or non-blob object."
            )


def _validate_result_path(path: str) -> None:
    candidate = Path(path)
    if (
        not path
        or candidate.is_absolute()
        or ".." in candidate.parts
        or ".git" in candidate.parts
        or "\x00" in path
    ):
        raise RuntimeError(f"Workspace result tree contains unsafe path: {path!r}.")


def validate_portable_path_collisions(paths: Iterable[str]) -> None:
    """Reject paths that collide on common portable filesystems.

    Raises:
        RuntimeError: If two distinct paths share the same portable form.
    """

    collision_paths: dict[str, str] = {}
    for path in paths:
        folded_path = portable_path_key(path)
        existing_path = collision_paths.setdefault(folded_path, path)
        if existing_path != path:
            raise RuntimeError(
                "Workspace result tree contains paths that collide under "
                f"case or Unicode normalization: {existing_path}, {path}."
            )

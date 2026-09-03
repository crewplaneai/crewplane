from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from pathlib import Path

from ..git import GitCommand, git
from .inspection import reserved_runtime_path


def validate_result_tree(
    checkout_root: Path,
    tree: str,
    project_root_relative_path: str = ".",
) -> None:
    command = git(checkout_root)
    records = command.zero_records(
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        tree,
    )
    paths: list[str] = []
    object_ids: list[str] = []
    for record in records:
        header, separator, path = record.partition("\t")
        if separator != "\t":
            raise RuntimeError("Workspace result tree contains an invalid entry.")
        fields = header.split()
        if len(fields) != 3:
            raise RuntimeError(
                "Workspace result tree contains invalid object metadata."
            )
        mode, object_type, object_id = fields
        if mode not in {"100644", "100755", "120000"}:
            raise RuntimeError(
                f"Workspace result tree contains unsupported mode {mode}."
            )
        if object_type != "blob":
            raise RuntimeError(
                f"Workspace result tree contains unsupported object type {object_type}."
            )
        _validate_result_path(path)
        if reserved_runtime_path(path, project_root_relative_path):
            raise RuntimeError(
                "Workspace result tree contains reserved runtime artifact paths."
            )
        paths.append(path)
        object_ids.append(object_id)
    _validate_result_blobs(command, object_ids)
    validate_portable_path_collisions(paths)


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


def _collision_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def validate_portable_path_collisions(paths: Iterable[str]) -> None:
    collision_paths: dict[str, str] = {}
    for path in paths:
        folded_path = _collision_key(path)
        existing_path = collision_paths.setdefault(folded_path, path)
        if existing_path != path:
            raise RuntimeError(
                "Workspace result tree contains paths that collide under "
                f"case or Unicode normalization: {existing_path}, {path}."
            )

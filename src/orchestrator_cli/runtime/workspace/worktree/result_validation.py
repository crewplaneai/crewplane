from __future__ import annotations

import unicodedata
from pathlib import Path

from ..git import git
from .inspection import reserved_runtime_path


def validate_result_tree(
    checkout_root: Path,
    tree: str,
    project_root_relative_path: str = ".",
) -> None:
    records = git(checkout_root).zero_records(
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        tree,
    )
    collision_paths: dict[str, str] = {}
    for record in records:
        header, separator, path = record.partition("\t")
        if separator != "\t":
            raise RuntimeError("Workspace result tree contains an invalid entry.")
        mode = header.split(" ", 1)[0]
        if mode == "160000":
            raise RuntimeError("Workspace result tree contains unsupported gitlinks.")
        if reserved_runtime_path(path, project_root_relative_path):
            raise RuntimeError(
                "Workspace result tree contains reserved runtime artifact paths."
            )
        folded_path = _collision_key(path)
        existing_path = collision_paths.setdefault(folded_path, path)
        if existing_path != path:
            raise RuntimeError(
                "Workspace result tree contains paths that collide under "
                f"case or Unicode normalization: {existing_path}, {path}."
            )


def _collision_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from crewplane.core.state_paths import RUNTIME_ARTIFACT_ROOTS, is_reserved_state_path


def estimated_tree_checkout_size_bytes(
    records: Iterable[str],
    project_root_relative_path: str,
    estimate_full_repository: bool = False,
) -> int | None:
    total = 0
    for record in records:
        header, separator, path = record.partition("\t")
        if separator != "\t":
            return None
        if not estimate_full_repository and not project_path_selected(
            path,
            project_root_relative_path,
        ):
            continue
        size_text = header.rsplit(" ", 1)[-1]
        if not size_text.isdigit():
            return None
        total += int(size_text)
    return total


def project_path_selected(path: str, project_root_relative_path: str) -> bool:
    if project_root_relative_path == ".":
        return True
    return path == project_root_relative_path or path.startswith(
        f"{project_root_relative_path}/"
    )


def estimated_working_tree_size_bytes(
    scan_root: Path, reserved_roots: tuple[Path, ...] | None = None
) -> int:
    if not scan_root.exists():
        return 0
    roots = reserved_roots or (scan_root,)
    total = 0
    for current_root, directories, files in scan_root.walk():
        directories[:] = [
            name
            for name in directories
            if name != ".git" and not reserved_checkout_path(current_root / name, roots)
        ]
        for file_name in files:
            try:
                total += (current_root / file_name).lstat().st_size
            except OSError:
                continue
    return total


def reserved_checkout_path(path: Path, reserved_roots: tuple[Path, ...]) -> bool:
    for scan_root in reserved_roots:
        try:
            relative = path.relative_to(scan_root).as_posix()
        except ValueError:
            continue
        if is_reserved_state_path(relative, RUNTIME_ARTIFACT_ROOTS):
            return True
    return False

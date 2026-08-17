from __future__ import annotations

import os
import stat
from contextlib import suppress
from pathlib import Path


def ensure_contained_directory(root: Path, relative_path: str) -> Path:
    """Create and return a non-symlink directory below ``root``."""

    _validate_relative_path(relative_path)
    _ensure_directory_root(root)
    current = root
    for part in Path(relative_path).parts:
        current = current / part
        _ensure_directory_component(current)
    return current


def contained_directory(root: Path, relative_path: str) -> Path | None:
    """Resolve a non-symlink directory below ``root`` when it exists."""

    _validate_relative_path(relative_path)
    if not _directory_root_exists_safely(root):
        return None
    current = root
    for part in Path(relative_path).parts:
        current = current / part
        try:
            component_stat = current.lstat()
        except FileNotFoundError:
            return None
        except PermissionError:
            raise
        except OSError as exc:
            raise ValueError(
                f"Cannot inspect contained directory '{current.as_posix()}'."
            ) from exc
        if stat.S_ISLNK(component_stat.st_mode) or not stat.S_ISDIR(
            component_stat.st_mode
        ):
            raise ValueError(
                f"Contained directory path is not a real directory: {current}"
            )
    return current


def contained_regular_file(root: Path, relative_path: str) -> Path | None:
    """Resolve a single-link regular file contained below ``root``."""

    raw_parts = relative_path.split("/")
    if (
        not relative_path
        or any(part in {"", ".", ".."} for part in raw_parts)
        or Path(relative_path).is_absolute()
    ):
        return None
    if _has_symlink_component(root):
        return None
    candidate = root
    for part in Path(*raw_parts).parts:
        candidate = candidate / part
        if _path_is_symlink(candidate):
            return None
    try:
        resolved = candidate.resolve(strict=True)
        root_resolved = root.resolve(strict=True)
    except PermissionError:
        raise
    except OSError:
        return None
    if not resolved.is_relative_to(root_resolved):
        return None
    try:
        file_stat = resolved.stat()
    except PermissionError:
        raise
    except OSError:
        return None
    if not resolved.is_file() or file_stat.st_nlink != 1:
        return None
    return resolved


def ensure_single_link_regular_file(path: Path) -> Path:
    """Create or return a single-link regular file without following links."""

    _ensure_directory_root(path.parent)
    safe_path = contained_regular_file(path.parent, path.name)
    if safe_path is not None:
        return safe_path
    try:
        path.lstat()
    except FileNotFoundError:
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            return ensure_single_link_regular_file(path)
        else:
            os.close(descriptor)
        safe_path = contained_regular_file(path.parent, path.name)
        if safe_path is not None:
            return safe_path
    raise ValueError(f"Path must be a single-link regular file: {path}")


def replace_contained_file(root: Path, relative_path: str, source: Path) -> Path:
    """Publish a same-filesystem file below a stable directory without clobbering."""

    _validate_relative_path(relative_path)
    parts = Path(relative_path).parts
    if not parts:
        raise ValueError("Contained replacement requires a file path.")
    source_stat = source.lstat()
    if not _is_single_link_regular_file(source_stat):
        raise ValueError(f"Publication source must be a single-link file: {source}")
    directory_fd = _open_contained_directory(root, parts[:-1])
    parent_path = root.joinpath(*parts[:-1])
    published = False
    try:
        _ensure_directory_handle_matches_path(directory_fd, parent_path)
        os.link(
            source,
            parts[-1],
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        published = True
        _ensure_entry_matches_source(directory_fd, parts[-1], source_stat)
        source.unlink()
        _ensure_regular_entry(directory_fd, parts[-1])
        _ensure_directory_handle_matches_path(directory_fd, parent_path)
    except BaseException:
        if published:
            _unlink_matching_entry(directory_fd, parts[-1], source_stat)
        raise
    finally:
        os.close(directory_fd)
    result = root.joinpath(*parts)
    if contained_regular_file(root, relative_path) is None:
        raise ValueError(f"Published file is not safely contained: {result}")
    return result


def _open_contained_directory(root: Path, relative_parts: tuple[str, ...]) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current_fd = os.open(root, flags)
    try:
        for part in relative_parts:
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
    except Exception:
        os.close(current_fd)
        raise
    return current_fd


def _ensure_directory_handle_matches_path(directory_fd: int, path: Path) -> None:
    opened_stat = os.fstat(directory_fd)
    current_stat = path.lstat()
    if (
        opened_stat.st_dev != current_stat.st_dev
        or opened_stat.st_ino != current_stat.st_ino
        or not stat.S_ISDIR(current_stat.st_mode)
    ):
        raise ValueError(f"Contained directory changed during publication: {path}")


def _ensure_entry_matches_source(
    directory_fd: int,
    name: str,
    source_stat: os.stat_result,
) -> None:
    entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(entry_stat.st_mode)
        or entry_stat.st_dev != source_stat.st_dev
        or entry_stat.st_ino != source_stat.st_ino
        or entry_stat.st_nlink != 2
    ):
        raise ValueError(f"Published entry does not match its source: {name}")


def _unlink_matching_entry(
    directory_fd: int,
    name: str,
    source_stat: os.stat_result,
) -> None:
    try:
        entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (
        entry_stat.st_dev == source_stat.st_dev
        and entry_stat.st_ino == source_stat.st_ino
    ):
        os.unlink(name, dir_fd=directory_fd)


def _ensure_regular_entry(directory_fd: int, name: str) -> None:
    entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if not _is_single_link_regular_file(entry_stat):
        raise ValueError(f"Published entry is not a single-link regular file: {name}")


def _is_single_link_regular_file(file_stat: os.stat_result) -> bool:
    return stat.S_ISREG(file_stat.st_mode) and file_stat.st_nlink == 1


def _validate_relative_path(relative_path: str) -> None:
    if relative_path in {"", "."}:
        return
    raw_parts = relative_path.split("/")
    if (
        any(part in {"", ".", ".."} for part in raw_parts)
        or Path(relative_path).is_absolute()
    ):
        raise ValueError("Contained paths must be safe relative POSIX paths.")


def _ensure_directory_root(root: Path) -> None:
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        root.mkdir(parents=True, exist_ok=True)
        root_stat = root.lstat()
    except PermissionError:
        raise
    except OSError as exc:
        raise ValueError(f"Cannot inspect directory root '{root}'.") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError(f"Directory root must be a real directory: {root}")


def _directory_root_exists_safely(root: Path) -> bool:
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return False
    except PermissionError:
        raise
    except OSError as exc:
        raise ValueError(f"Cannot inspect directory root '{root}'.") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError(f"Directory root must be a real directory: {root}")
    return True


def _ensure_directory_component(path: Path) -> None:
    try:
        component_stat = path.lstat()
    except FileNotFoundError:
        with suppress(FileExistsError):
            path.mkdir()
        component_stat = path.lstat()
    except PermissionError:
        raise
    except OSError as exc:
        raise ValueError(f"Cannot inspect directory component '{path}'.") from exc
    if stat.S_ISLNK(component_stat.st_mode) or not stat.S_ISDIR(component_stat.st_mode):
        raise ValueError(f"Directory component must be a real directory: {path}")


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor) if path.is_absolute() else Path()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current = current / part
        if _path_is_symlink(current):
            return True
    return False


def _path_is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except PermissionError:
        raise
    except OSError:
        return False

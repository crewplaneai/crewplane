"""Contained operations whose directory and source handles stay protected."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from .windows_file_handles import (
    DELETE,
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_CREATE,
    FILE_LIST_DIRECTORY,
    FILE_OPEN_IF,
    FILE_READ_ATTRIBUTES,
    FILE_SHARE_READ,
    FILE_SHARE_WRITE,
    FILE_TRAVERSE,
    GENERIC_READ,
    GENERIC_WRITE,
    FileHandle,
    open_handle,
)


@contextmanager
def protected_directory(
    path: Path, create: bool = False, list_entries: bool = False
) -> Iterator[FileHandle]:
    absolute = Path(os.path.abspath(path))
    with _path_errors(), ExitStack() as handles:
        directories = []
        access = FILE_READ_ATTRIBUTES | FILE_TRAVERSE
        listing_access = access | FILE_LIST_DIRECTORY if list_entries else access
        share = FILE_SHARE_READ | FILE_SHARE_WRITE
        handle = open_handle(
            Path(absolute.anchor),
            listing_access if len(absolute.parts) == 1 else access,
            share,
        )
        handles.callback(handle.close)
        handle.validate(directory=True)
        directories.append(handle)
        for index, part in enumerate(absolute.parts[1:], start=1):
            handle = handle.open_child(
                part,
                listing_access if index == len(absolute.parts) - 1 else access,
                share,
                FILE_OPEN_IF if create else 1,
                directory=True,
            )
            handles.callback(handle.close)
            handle.validate(directory=True)
            directories.append(handle)
        yield handle
        for directory in directories:
            directory.validate(directory=True)


@contextmanager
def _path_errors() -> Iterator[None]:
    try:
        yield
    except OSError as exc:
        code = getattr(exc, "winerror", None)
        if code == 206 or (code == 3 and len(str(exc.filename or "")) >= 260):
            exc.add_note(
                "Enable Windows long-path support or shorten the project path."
            )
        raise


@contextmanager
def protected_file(path: Path, links: int = 1) -> Iterator[FileHandle]:
    with protected_directory(path.parent) as parent:
        handle = parent.open_child(path.name, GENERIC_READ)
        try:
            handle.validate(directory=False, links=links)
            yield handle
        finally:
            handle.close()


@contextmanager
def open_regular_file(path: Path) -> Iterator[int]:
    with protected_file(path) as handle:
        descriptor = handle.into_descriptor()
        try:
            yield descriptor
        finally:
            os.close(descriptor)


def contained_directory(
    root: Path, parts: tuple[str, ...], create: bool = False
) -> Path | None:
    try:
        with protected_directory(root.joinpath(*parts), create) as path:
            return path.path
    except FileNotFoundError:
        if create:
            raise
        return None


def contained_regular_file(root: Path, parts: tuple[str, ...]) -> Path | None:
    path = root.joinpath(*parts)
    try:
        with protected_file(path):
            return Path(os.path.abspath(path))
    except (FileNotFoundError, ValueError):
        return None


def ensure_regular_file(path: Path) -> Path:
    with protected_directory(path.parent, create=True) as parent:
        handle = parent.open_child(path.name, GENERIC_READ, disposition=FILE_OPEN_IF)
        try:
            handle.validate(directory=False)
            return path
        finally:
            handle.close()


@contextmanager
def open_writable_file(path: Path, append: bool = False) -> Iterator[int]:
    with protected_directory(path.parent) as parent:
        handle = parent.open_child(
            path.name, GENERIC_READ | GENERIC_WRITE, disposition=FILE_OPEN_IF
        )
        try:
            handle.validate(directory=False)
            descriptor = handle.into_descriptor(
                os.O_RDWR | (os.O_APPEND if append else 0)
            )
            try:
                if not append:
                    os.ftruncate(descriptor, 0)
                yield descriptor
            finally:
                os.close(descriptor)
        finally:
            handle.close()


def delete_matching_file(
    path: Path, identity: tuple[int, int], required: bool = False
) -> None:
    """Delete only the owned entry while its parent remains protected."""
    with protected_directory(path.parent) as parent:
        delete_matching_entry(parent, path.name, identity, required)


def delete_matching_entry(
    parent: FileHandle, name: str, identity: tuple[int, int], required: bool = False
) -> None:
    try:
        handle = parent.open_child(name, DELETE | FILE_READ_ATTRIBUTES)
    except FileNotFoundError:
        if required:
            raise
        return
    try:
        info = handle.information()
        if info.identity != identity or info.attributes & 0x400:
            if required:
                raise ValueError(f"Publication entry changed identity: {handle.path}")
            return
        handle.delete()
    finally:
        handle.close()


def reset_directory(path: Path) -> None:
    """Replace a snapshot directory while protecting every mutated component."""
    with protected_directory(path.parent, create=True) as parent:
        try:
            handle = parent.open_child(
                path.name,
                DELETE | FILE_READ_ATTRIBUTES | FILE_LIST_DIRECTORY,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
            )
        except FileNotFoundError:
            pass
        else:
            try:
                handle.validate(directory=True)
                _delete_directory_contents(handle)
                handle.delete()
            finally:
                handle.close()
        handle = parent.open_child(
            path.name,
            FILE_READ_ATTRIBUTES | FILE_TRAVERSE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            FILE_CREATE,
            directory=True,
        )
        try:
            handle.validate(directory=True)
        finally:
            handle.close()


def _delete_directory_contents(parent: FileHandle) -> None:
    for name in parent.entry_names():
        handle = parent.open_child(
            name,
            DELETE | FILE_READ_ATTRIBUTES | FILE_LIST_DIRECTORY,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
        )
        try:
            directory = bool(handle.information().attributes & FILE_ATTRIBUTE_DIRECTORY)
            handle.validate(directory=directory)
            if directory:
                _delete_directory_contents(handle)
            handle.delete()
        finally:
            handle.close()


@contextmanager
def temporary_binary_file(
    directory: Path, prefix: str, suffix: str = ""
) -> Iterator[tuple[Path, BinaryIO]]:
    with protected_directory(directory) as parent:
        name = f"{prefix}{uuid4().hex}{suffix}"
        native = parent.open_child(
            name,
            GENERIC_READ | GENERIC_WRITE | DELETE,
            disposition=FILE_CREATE,
            directory=False,
        )
        try:
            identity = native.validate(directory=False).identity
            descriptor = native.into_descriptor(os.O_RDWR)
        except BaseException:
            native.delete()
            raise
        finally:
            native.close()
        with os.fdopen(descriptor, "wb") as stream:
            try:
                yield parent.path / name, stream
            finally:
                stream.close()
                delete_matching_entry(parent, name, identity)


def rename_contained_file(
    source: Path, target: Path, identity: tuple[int, int]
) -> None:
    with (
        protected_directory(target.parent) as parent,
        protected_directory(source.parent) as source_parent,
    ):
        handle = source_parent.open_child(source.name, DELETE | FILE_READ_ATTRIBUTES)
        try:
            if handle.validate(directory=False).identity != identity:
                raise ValueError(f"Metadata temporary entry changed identity: {source}")
            handle.publish_entry(parent, target.name, rename=True)
        finally:
            handle.close()


def replace_contained_file(root: Path, parts: tuple[str, ...], source: Path) -> Path:
    target = root.joinpath(*parts)
    with (
        protected_directory(target.parent) as parent,
        protected_directory(source.parent),
    ):
        published = False
        identity: tuple[int, int] | None = None
        try:
            with protected_file(source) as handle:
                identity = handle.information().identity
                handle.publish_entry(parent, target.name)
                published = True
                with protected_file(target, links=2) as destination:
                    if destination.information().identity != identity:
                        raise ValueError(
                            f"Published entry does not match its source: {target}"
                        )
            # Both read handles must close before deleting one of the two links.
            delete_matching_file(source, identity, required=True)
            with protected_file(target) as destination:
                if destination.information().identity != identity:
                    raise ValueError(f"Publication changed identity: {target}")
        except BaseException:
            if published and identity is not None:
                delete_matching_file(target, identity)
            raise
    return target

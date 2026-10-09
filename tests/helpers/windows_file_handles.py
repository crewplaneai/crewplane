"""Local directory-descriptor surrogate for Windows handle operation tests."""

import os
import stat
from pathlib import Path
from types import SimpleNamespace


class LocalHandle:
    def __init__(self, path, calls=None, parent=None, descriptor=None):
        self.path = Path(path)
        self.calls = [] if calls is None else calls
        self.parent = parent
        self.closed = False
        self.descriptor = descriptor
        if descriptor is None and os.name != "nt":
            self.descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)

    def information(self):
        metadata = (
            os.fstat(self.descriptor)
            if self.descriptor is not None
            else self.path.lstat()
        )
        return SimpleNamespace(
            identity=(metadata.st_dev, metadata.st_ino),
            attributes=(0x10 if stat.S_ISDIR(metadata.st_mode) else 0)
            | (0x400 if self.path.is_symlink() else 0),
        )

    def validate(self, directory, links=1):
        metadata = (
            os.fstat(self.descriptor)
            if self.descriptor is not None
            else self.path.lstat()
        )
        if (
            self.path.is_symlink()
            or stat.S_ISDIR(metadata.st_mode) != directory
            or (not directory and metadata.st_nlink != links)
        ):
            raise ValueError("unsafe entry")
        return self.information()

    def open_child(self, name, access=0x80, share=1, disposition=1, directory=None):
        path = self.path / name
        self.calls.append(("open", path, access, share))
        if directory and disposition in {2, 3}:
            try:
                if self.descriptor is None:
                    path.mkdir()
                else:
                    os.mkdir(name, dir_fd=self.descriptor)
            except FileExistsError:
                if disposition == 2:
                    raise
        flags = os.O_RDWR if access & 0x40000000 else os.O_RDONLY
        if not directory and disposition in {2, 3}:
            flags |= os.O_CREAT | (os.O_EXCL if disposition == 2 else 0)
        if self.descriptor is None:
            if not directory and disposition in {2, 3}:
                descriptor = os.open(path, flags | getattr(os, "O_BINARY", 0), 0o600)
                return LocalHandle(path, self.calls, self, descriptor)
            path.lstat()
            return LocalHandle(path, self.calls, self)
        descriptor = os.open(name, flags | os.O_NOFOLLOW, 0o600, dir_fd=self.descriptor)
        return LocalHandle(path, self.calls, self, descriptor)

    def entry_names(self):
        return iter(
            os.listdir(self.path if self.descriptor is None else self.descriptor)
        )

    def delete(self):
        self.calls.append(("delete", self.path))
        directory = bool(self.information().attributes & 0x10)
        operation = os.rmdir if directory else os.unlink
        if self.parent is not None and self.parent.descriptor is not None:
            operation(self.path.name, dir_fd=self.parent.descriptor)
        else:
            operation(self.path)

    def publish_entry(self, parent, name, rename=False):
        if self.parent is None or self.parent.descriptor is None:
            if rename:
                self.path.replace(parent.path / name)
            else:
                os.link(self.path, parent.path / name)
            return
        if rename:
            os.rename(
                self.path.name,
                name,
                src_dir_fd=self.parent.descriptor,
                dst_dir_fd=parent.descriptor,
            )
        else:
            os.link(
                self.path.name,
                name,
                src_dir_fd=self.parent.descriptor,
                dst_dir_fd=parent.descriptor,
                follow_symlinks=False,
            )

    def into_descriptor(self, flags=os.O_RDONLY):
        if self.descriptor is None:
            return os.open(self.path, flags | getattr(os, "O_BINARY", 0))
        descriptor, self.descriptor = self.descriptor, None
        if flags & os.O_APPEND:
            os.lseek(descriptor, 0, os.SEEK_END)
        return descriptor

    def close(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None
        self.closed = True

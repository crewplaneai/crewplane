from __future__ import annotations

import os
from pathlib import Path


def publish_bytes(
    path: Path,
    payload: bytes,
    ensure_parent: bool,
    temporary_names: tuple[str, str],
    replace: bool,
) -> Path:
    from crewplane.architecture.safe_files_windows import (
        protected_directory,
        rename_contained_file,
        replace_contained_file,
        temporary_binary_file,
    )
    from crewplane.architecture.windows_file_handles import descriptor_identity

    with protected_directory(path.parent, create=ensure_parent):
        phase = "create temporary file"
        try:
            with temporary_binary_file(
                path.parent, temporary_names[0], temporary_names[1]
            ) as (temporary_path, stream):
                phase = "write temporary file"
                stream.write(payload)
                stream.flush()
                phase = "sync temporary file"
                os.fsync(stream.fileno())
                identity = descriptor_identity(stream.fileno(), temporary_path)
                stream.close()
                phase = "replace target" if replace else "publish target link"
                if replace:
                    rename_contained_file(temporary_path, path, identity)
                else:
                    replace_contained_file(path.parent, (path.name,), temporary_path)
                return path
        except OSError as exc:
            exc.add_note(f"Atomic publication failed for '{path}' during {phase}.")
            raise

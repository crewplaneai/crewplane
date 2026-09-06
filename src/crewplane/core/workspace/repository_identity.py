from __future__ import annotations

import hashlib
from pathlib import Path


def workspace_repository_id(
    common_git_dir: Path,
    project_root: Path,
    object_format: str,
) -> str:
    payload = "|".join(
        [
            common_git_dir.resolve(strict=False).as_posix(),
            project_root.resolve(strict=False).as_posix(),
            object_format,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

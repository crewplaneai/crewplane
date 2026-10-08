"""Narrow platform requirements for unsupported features and native mechanisms."""

import os
from pathlib import Path

import pytest

from crewplane.core.platform import is_native_windows

requires_posix = pytest.mark.skipif(
    os.name != "posix",
    reason="Requires POSIX implementation; native Windows has separate coverage",
)
requires_workspace_support = pytest.mark.skipif(
    is_native_windows(), reason="Managed workspaces are unsupported on native Windows"
)
requires_resume_support = pytest.mark.skipif(
    is_native_windows(),
    reason="Interrupted-run resume is unsupported on native Windows",
)
requires_lock_recovery = pytest.mark.skipif(
    is_native_windows(),
    reason="Automatic lock recovery is unsupported on native Windows",
)
requires_self_update = pytest.mark.skipif(
    is_native_windows(), reason="Native Windows self-update is unsupported"
)
requires_tmux_support = pytest.mark.skipif(
    is_native_windows(),
    reason="The tmux live dashboard is unsupported on native Windows",
)


def symlink_or_skip(
    link: Path, target: str | Path, target_is_directory: bool = False
) -> None:
    """Create a fixture link, recording unavailable Windows creation privilege."""
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows host lacks symlink creation privilege")
        raise


def extended_file_test_root(path: Path) -> Path:
    """Exercise long file paths without requiring host-wide registry changes."""
    if os.name != "nt":
        return path
    absolute = str(path.resolve())
    return Path(absolute if absolute.startswith("\\\\?\\") else "\\\\?\\" + absolute)

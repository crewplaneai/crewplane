from __future__ import annotations

import subprocess
from pathlib import Path

from ..git import GitCommand, git


def checked_ref(checkout_root: Path, ref: str) -> str:
    normalized = git(checkout_root).text("check-ref-format", "--normalize", ref)
    if normalized != ref:
        raise RuntimeError(f"Unsafe workspace ref name: {ref}")
    return normalized


def direct_ref_oid(command: GitCommand, ref_name: str, description: str) -> str | None:
    try:
        target = command.text("symbolic-ref", "-q", ref_name)
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1:
            raise
    else:
        raise RuntimeError(
            f"Workspace {description} ref is symbolic and was retained: "
            f"{ref_name} -> {target}."
        )
    try:
        return command.text("rev-parse", "--verify", ref_name)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 128:
            return None
        raise

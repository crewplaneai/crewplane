from __future__ import annotations

from pathlib import Path

from ..git import git


def checked_ref(checkout_root: Path, ref: str) -> str:
    normalized = git(checkout_root).text("check-ref-format", "--normalize", ref)
    if normalized != ref:
        raise RuntimeError(f"Unsafe workspace ref name: {ref}")
    return normalized

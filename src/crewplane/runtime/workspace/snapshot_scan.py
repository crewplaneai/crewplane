from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path

from . import snapshot_scan_common


def snapshot_scanner() -> Callable[
    [Path, snapshot_scan_common.WorkspaceSnapshotBudget], dict[str, str]
]:
    if os.name == "nt":
        from .snapshot_scan_windows import scan_snapshot
    else:
        from .snapshot_scan_posix import scan_snapshot
    return scan_snapshot


def snapshot_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for relative_path, entry_digest in snapshot_entries(root).items():
        digest.update(f"{relative_path}\0{entry_digest}\0".encode())
    return digest.hexdigest()


def snapshot_entries(
    root: Path,
    policy: snapshot_scan_common.WorkspaceSnapshotPolicy | None = None,
) -> dict[str, str]:
    resolved_policy = policy or snapshot_scan_common.WorkspaceSnapshotPolicy()
    budget = snapshot_scan_common.WorkspaceSnapshotBudget(
        policy=resolved_policy,
        started_at=resolved_policy.clock(),
    )
    return dict(sorted(snapshot_scanner()(root, budget).items()))

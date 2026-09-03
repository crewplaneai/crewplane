from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..git import git
from .refs import checked_ref

if TYPE_CHECKING:
    from .types import WorktreeSourceRef


@dataclass(frozen=True)
class ProtectedRefSnapshot:
    scopes: tuple[str, ...]
    refs: tuple[tuple[str, str], ...]


def protected_ref_snapshot(repo_root: Path) -> ProtectedRefSnapshot:
    del repo_root
    return ProtectedRefSnapshot(scopes=(), refs=())


def protected_ref_snapshot_for_scopes(
    repo_root: Path,
    scopes: tuple[str, ...],
) -> ProtectedRefSnapshot:
    refs = [
        (scope, object_id)
        for scope in sorted(scopes)
        if (object_id := _ref_oid(repo_root, scope)) is not None
    ]
    return ProtectedRefSnapshot(
        scopes=tuple(sorted(scopes)),
        refs=tuple(sorted(refs)),
    )


def protected_ref_snapshot_for_source(
    git_top_level: str,
    protected_ref_scopes: tuple[str, ...] | None,
    source_ref: WorktreeSourceRef | None = None,
) -> ProtectedRefSnapshot:
    """Capture an explicitly validated execution-local ref subset."""

    repo_root = Path(git_top_level)
    consumed_refs = _consumed_ref_expectations(repo_root, source_ref)
    if protected_ref_scopes is None and not consumed_refs:
        return protected_ref_snapshot(repo_root)
    checked_scopes = {
        checked_ref(repo_root, scope) for scope in protected_ref_scopes or ()
    }
    checked_scopes.update(consumed_refs)
    snapshot = protected_ref_snapshot_for_scopes(
        repo_root,
        tuple(sorted(checked_scopes)),
    )
    _reject_consumed_ref_mismatch(snapshot, consumed_refs)
    return snapshot


def reject_protected_ref_drift(
    repo_root: Path,
    expected: ProtectedRefSnapshot,
) -> None:
    current = protected_ref_snapshot_for_scopes(repo_root, expected.scopes)
    if current.refs == expected.refs:
        return
    expected_refs = dict(expected.refs)
    current_refs = dict(current.refs)
    added = len(set(current_refs) - set(expected_refs))
    removed = len(set(expected_refs) - set(current_refs))
    changed = sum(
        1
        for ref_name, object_id in current_refs.items()
        if ref_name in expected_refs and expected_refs[ref_name] != object_id
    )
    raise RuntimeError(
        "Workspace provider modified protected crewplane Git refs "
        f"(added={added}, removed={removed}, changed={changed})."
    )


def _ref_oid(repo_root: Path, ref_name: str) -> str | None:
    try:
        return git(repo_root).text("rev-parse", "--verify", ref_name)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 128:
            return None
        raise


def _consumed_ref_expectations(
    repo_root: Path,
    source_ref: WorktreeSourceRef | None,
) -> dict[str, str]:
    if source_ref is None:
        return {}
    expectations: dict[str, str] = {}
    pending = [source_ref]
    while pending:
        current = pending.pop()
        if current.bundle_ref is not None:
            ref_name = checked_ref(repo_root, current.bundle_ref)
            prior = expectations.setdefault(ref_name, current.source_commit)
            if prior != current.source_commit:
                raise RuntimeError(
                    "Workspace lineage descriptors assign one consumed ref to "
                    "multiple source commits."
                )
        pending.extend(current.upstream_sources)
    return expectations


def _reject_consumed_ref_mismatch(
    snapshot: ProtectedRefSnapshot,
    expectations: dict[str, str],
) -> None:
    actual_refs = dict(snapshot.refs)
    mismatched = tuple(
        ref_name
        for ref_name, expected_oid in expectations.items()
        if ref_name in actual_refs and actual_refs[ref_name] != expected_oid
    )
    if mismatched:
        raise RuntimeError(
            "Workspace consumed lineage ref does not match its recorded source "
            f"commit: {', '.join(sorted(mismatched))}."
        )

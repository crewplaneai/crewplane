from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..git import git

PROTECTED_REF_PREFIX = "refs/orchestrator-cli"


@dataclass(frozen=True)
class ProtectedRefSnapshot:
    scopes: tuple[str, ...]
    refs: tuple[tuple[str, str], ...]


def protected_ref_snapshot(repo_root: Path) -> ProtectedRefSnapshot:
    return protected_ref_snapshot_for_scopes(repo_root, (PROTECTED_REF_PREFIX,))


def protected_ref_snapshot_for_scopes(
    repo_root: Path,
    scopes: tuple[str, ...],
) -> ProtectedRefSnapshot:
    if not scopes:
        raise RuntimeError("Protected ref snapshot requires at least one scope.")
    records = git(repo_root).text(
        "for-each-ref",
        "--format=%(refname)%09%(objectname)",
        *scopes,
    )
    refs: list[tuple[str, str]] = []
    for line in records.splitlines():
        ref_name, separator, object_id = line.partition("\t")
        if separator != "\t" or not ref_name or not object_id:
            raise RuntimeError("Git returned an invalid protected ref record.")
        refs.append((ref_name, object_id))
    return ProtectedRefSnapshot(
        scopes=tuple(sorted(scopes)),
        refs=tuple(sorted(refs)),
    )


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
        "Workspace provider modified protected orchestrator Git refs "
        f"(added={added}, removed={removed}, changed={changed})."
    )

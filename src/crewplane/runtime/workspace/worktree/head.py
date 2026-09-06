from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..git import GitCommand, git


@dataclass(frozen=True)
class WorktreeHeadProof:
    commit: str
    attached_ref: str | None
    attached_ref_oid: str | None

    @property
    def was_attached(self) -> bool:
        return self.attached_ref is not None


class AttachedWorktreeRequiresDisposal(RuntimeError):
    """Signals that an attached checkout was preserved and may only be disposed."""


def prove_detached_head(
    checkout_root: Path,
    expected_commit: str,
    command: GitCommand | None = None,
) -> WorktreeHeadProof:
    command = command or git(checkout_root)
    attached_ref = _symbolic_head(command)
    head_commit = command.text("rev-parse", "HEAD^{commit}")
    if attached_ref is not None:
        raise RuntimeError(
            "Workspace HEAD is attached; retry and result capture require an "
            "already-detached checkout."
        )
    if head_commit != expected_commit:
        raise RuntimeError(
            "Workspace detached HEAD does not match the recorded source commit."
        )
    return WorktreeHeadProof(head_commit, None, None)


def detach_attached_head_for_disposal(
    checkout_root: Path,
    command: GitCommand | None = None,
) -> WorktreeHeadProof:
    command = command or git(checkout_root)
    attached_ref = _symbolic_head(command)
    head_commit = command.text("rev-parse", "HEAD^{commit}")
    if attached_ref is None:
        return WorktreeHeadProof(head_commit, None, None)
    branch_oid = command.text("rev-parse", f"{attached_ref}^{{commit}}")
    if branch_oid != head_commit:
        raise RuntimeError(
            "Workspace attached branch and HEAD do not resolve to the same commit."
        )
    command.run("checkout", "--detach", branch_oid)
    if _symbolic_head(command) is not None:
        raise RuntimeError(
            "Workspace branch-preserving detachment did not detach HEAD."
        )
    if command.text("rev-parse", "HEAD^{commit}") != branch_oid:
        raise RuntimeError("Workspace branch-preserving detachment moved HEAD.")
    if command.text("rev-parse", f"{attached_ref}^{{commit}}") != branch_oid:
        raise RuntimeError("Workspace branch OID changed during safe detachment.")
    return WorktreeHeadProof(branch_oid, attached_ref, branch_oid)


def reject_attached_head_after_safe_detachment(
    checkout_root: Path,
    command: GitCommand | None = None,
) -> None:
    proof = detach_attached_head_for_disposal(checkout_root, command)
    if proof.was_attached:
        raise AttachedWorktreeRequiresDisposal(
            "Attached workspace HEAD was safely detached without moving its branch; "
            "the checkout must be disposed instead of reset or reused."
        )


def advance_detached_head_for_reuse(
    checkout_root: Path,
    expected_commit: str,
    result_commit: str,
) -> None:
    command = git(checkout_root, timeout_seconds=30.0)
    prove_detached_head(checkout_root, expected_commit, command)
    command.run("reset", "--soft", result_commit)
    prove_detached_head(checkout_root, result_commit, command)


def _symbolic_head(command: GitCommand) -> str | None:
    try:
        ref = command.text("symbolic-ref", "-q", "HEAD")
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return None
        raise
    return ref or None

from __future__ import annotations

import os
from pathlib import Path

import pytest

from crewplane.core.workspace.checkout_size import (
    estimated_tree_checkout_size_bytes,
    estimated_working_tree_size_bytes,
)
from crewplane.core.workspace.policy import WorktreeDeclaration, validate_worktree_name


@pytest.mark.parametrize(
    "record", ["missing separator", "100644 blob abcd unknown\tcontext.md"]
)
def test_checkout_size_estimate_returns_unknown_for_malformed_git_records(
    record: str,
) -> None:
    assert (
        estimated_tree_checkout_size_bytes(
            ["100644 blob 1234 4\tknown.md", record], "."
        )
        is None
    )


def test_working_tree_estimate_handles_missing_root(tmp_path: Path) -> None:
    assert estimated_working_tree_size_bytes(tmp_path / "missing") == 0


def test_working_tree_estimate_keeps_readable_files_when_an_entry_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "readable.md").write_bytes(b"12345")
    disappearing = tmp_path / "disappearing.md"
    disappearing.write_bytes(b"unavailable")
    original_lstat = Path.lstat

    def lstat(path: Path) -> os.stat_result:
        if path == disappearing:
            raise FileNotFoundError("concurrently removed")
        return original_lstat(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", lstat)
        size = estimated_working_tree_size_bytes(tmp_path)

    assert size == 5


@pytest.mark.parametrize(
    ("name", "message"),
    [("none", "reserved"), ("Feature", "must match"), (" ", "must match")],
)
def test_logical_worktree_names_reject_reserved_and_invalid_identifiers(
    name: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_worktree_name(name)


def test_worktree_declaration_accepts_explicitly_unset_optional_fields() -> None:
    declaration = WorktreeDeclaration.model_validate(
        {"kind": "worktree", "setup_profile": None, "branch_name": None}
    )

    assert declaration.setup_profile is None
    assert declaration.branch_name is None
    assert declaration.create_branch is False


def test_worktree_declaration_requires_export_for_an_explicit_branch() -> None:
    with pytest.raises(ValueError, match="branch_name requires create_branch"):
        WorktreeDeclaration.model_validate(
            {"kind": "worktree", "branch_name": "review/output"}
        )

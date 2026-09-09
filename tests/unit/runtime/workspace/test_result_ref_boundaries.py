from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.core.workspace.invocation_identity import invocation_slug
from crewplane.core.workspace.naming import result_ref_names
from crewplane.runtime.workspace.git import GitCommand
from crewplane.runtime.workspace.worktree.ref_publication import (
    reconcile_result_ref_publication,
)
from crewplane.runtime.workspace.worktree.result_validation import validate_result_tree


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (b"missing-tab", "invalid entry"),
        (b"blob\tfile", "invalid object metadata"),
        (b"100644 tree abc\tfile", "unsupported object type"),
        (b"100644 blob abc\t/absolute", "unsafe path"),
        (b"100644 blob abc\t../parent", "unsafe path"),
        (b"100644 blob abc\t.git/config", "unsafe path"),
    ],
)
def test_result_capture_rejects_malformed_git_tree_entries(
    tmp_path: Path, record: bytes, message: str
) -> None:
    result = subprocess.CompletedProcess(["git"], 0, record + b"\0", b"")
    with (
        patch.object(GitCommand, "run", return_value=result),
        pytest.raises(RuntimeError, match=message),
    ):
        validate_result_tree(tmp_path, "a" * 40)


@pytest.mark.parametrize(
    "object_output",
    [b"", b"abc blob\nabc blob\n", b"abc\n", b"different blob\n", b"abc tree\n"],
)
def test_result_capture_requires_complete_matching_blob_verification(
    tmp_path: Path, object_output: bytes
) -> None:
    tree = subprocess.CompletedProcess(["git"], 0, b"100644 blob abc\tfile.txt\0", b"")
    objects = subprocess.CompletedProcess(["git"], 0, object_output, b"")
    with (
        patch.object(GitCommand, "run", return_value=tree),
        patch.object(GitCommand, "run_with_input", return_value=objects) as verify,
        pytest.raises(
            RuntimeError, match="verification was incomplete|missing or non-blob"
        ),
    ):
        validate_result_tree(tmp_path, "a" * 40)
    assert verify.call_args.args[0] == b"abc\n"


def test_empty_result_tree_needs_no_blob_lookup(tmp_path: Path) -> None:
    empty = subprocess.CompletedProcess(["git"], 0, b"", b"")
    with (
        patch.object(GitCommand, "run", return_value=empty),
        patch.object(GitCommand, "run_with_input") as verify,
    ):
        validate_result_tree(tmp_path, "a" * 40)
    verify.assert_not_called()


def publication_payload(repo: Path) -> dict[str, object]:
    names = result_ref_names("run", "node", invocation_slug("node", "worker", None, 1))
    return {
        "run_key_name": "run",
        "node_id": "node",
        "task_id": "worker",
        "round_num": 1,
        "audit_round_num": None,
        "git": {"git_top_level": str(repo), "common_git_dir": str(repo / ".git")},
        "ref_publication": {
            "phase": "prepared",
            "destinations": {
                label: {"name": name, "target_oid": "a" * 40, "expected_old_oid": None}
                for label, name in zip(("candidate", "result"), names, strict=True)
            },
        },
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("git", None, "lacks repository evidence"),
        ("git", {}, "repository identity changed"),
        ("run_key_name", "", "lacks run_key_name identity"),
        ("node_id", "", "lacks node_id identity"),
        ("task_id", "", "lacks task_id identity"),
        ("round_num", True, "lacks round identity"),
        ("round_num", "1", "lacks round identity"),
        ("audit_round_num", True, "invalid audit identity"),
        ("audit_round_num", "1", "invalid audit identity"),
    ],
)
def test_result_ref_reconciliation_rejects_unowned_publication_evidence(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    (tmp_path / ".git").mkdir()
    state = tmp_path / "workspace-state.json"
    payload = publication_payload(tmp_path)
    payload[field] = value
    state.write_text(json.dumps(payload), encoding="utf-8")
    with (
        patch.object(GitCommand, "run") as command,
        pytest.raises(RuntimeError, match=message),
    ):
        reconcile_result_ref_publication(state, tmp_path, tmp_path / ".git")
    command.assert_not_called()
    assert json.loads(state.read_text(encoding="utf-8")) == payload


def test_result_ref_reconciliation_rejects_ref_names_outside_owner_scope(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    state = tmp_path / "workspace-state.json"
    payload = publication_payload(tmp_path)
    payload["task_id"] = "different-worker"
    state.write_text(json.dumps(payload), encoding="utf-8")
    with (
        patch.object(GitCommand, "run") as command,
        pytest.raises(RuntimeError, match="escape their invocation scope"),
    ):
        reconcile_result_ref_publication(state, tmp_path, tmp_path / ".git")
    command.assert_not_called()

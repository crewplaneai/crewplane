from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from tests.helpers.platforms import symlink_or_skip

StatusMutator = Callable[[dict[str, object], Path], None]


def valid_status_payload(node_id: str = "stage") -> dict[str, object]:
    return {
        "node_id": node_id,
        "executed_audit_rounds": 1,
        "attempted_local_round_num": 2,
        "final_local_round_num": 2,
        "invalid_candidate_round_count": 0,
        "no_progress_round_count": 0,
        "artifact_drift_warning_count": 0,
        "consensus_reached": True,
        "continued_after_consensus_exhaustion": False,
        "canonical_executor_outputs": [
            {
                "task_id": "executor",
                "provider": "codex",
                "role": "executor",
                "path": "executor_round2.md",
                "sha256": hashlib.sha256(b"executor").hexdigest(),
                "size_bytes": len(b"executor"),
                "audit_round_num": None,
                "round_num": 2,
            }
        ],
        "reviewer_outputs": [
            {
                "task_id": "reviewer",
                "provider": "claude",
                "role": "reviewer",
                "path": "reviewer_round2.md",
                "sha256": hashlib.sha256(b"reviewer").hexdigest(),
                "size_bytes": len(b"reviewer"),
                "audit_round_num": None,
                "round_num": 2,
            }
        ],
    }


def write_status(stage_dir: Path, payload: dict[str, object] | str) -> None:
    status_dir = stage_dir / "review-state"
    status_dir.mkdir(parents=True)
    content = payload if isinstance(payload, str) else json.dumps(payload)
    (status_dir / "review-loop-status.json").write_text(content, encoding="utf-8")


def create_referenced_outputs(stage_dir: Path) -> None:
    (stage_dir / "executor_round2.md").write_text("executor", encoding="utf-8")
    (stage_dir / "reviewer_round2.md").write_text("reviewer", encoding="utf-8")


def malformed_json(stage_dir: Path) -> None:
    write_status(stage_dir, "{")


def non_object_json(stage_dir: Path) -> None:
    write_status(stage_dir, "[]")


def missing_node_id(payload: dict[str, object], stage_dir: Path) -> None:
    payload.pop("node_id")
    write_status(stage_dir, payload)


def empty_node_id(payload: dict[str, object], stage_dir: Path) -> None:
    payload["node_id"] = ""
    write_status(stage_dir, payload)


def mismatched_node_id(payload: dict[str, object], stage_dir: Path) -> None:
    payload["node_id"] = "other"
    write_status(stage_dir, payload)


def negative_counter(payload: dict[str, object], stage_dir: Path) -> None:
    payload["executed_audit_rounds"] = -1
    write_status(stage_dir, payload)


def boolean_counter(payload: dict[str, object], stage_dir: Path) -> None:
    payload["final_local_round_num"] = True
    write_status(stage_dir, payload)


def missing_counter(payload: dict[str, object], stage_dir: Path) -> None:
    payload.pop("invalid_candidate_round_count")
    write_status(stage_dir, payload)


def missing_boolean(payload: dict[str, object], stage_dir: Path) -> None:
    payload.pop("consensus_reached")
    write_status(stage_dir, payload)


def wrong_boolean(payload: dict[str, object], stage_dir: Path) -> None:
    payload["continued_after_consensus_exhaustion"] = "false"
    write_status(stage_dir, payload)


def wrong_list_type(payload: dict[str, object], stage_dir: Path) -> None:
    payload["canonical_executor_outputs"] = {}
    write_status(stage_dir, payload)


def missing_list(payload: dict[str, object], stage_dir: Path) -> None:
    payload.pop("reviewer_outputs")
    write_status(stage_dir, payload)


def non_object_entry(payload: dict[str, object], stage_dir: Path) -> None:
    payload["reviewer_outputs"] = ["bad"]
    write_status(stage_dir, payload)


def empty_entry_string(payload: dict[str, object], stage_dir: Path) -> None:
    payload["canonical_executor_outputs"] = [
        {"task_id": "", "provider": "codex", "role": "executor", "path": "x.md"}
    ]
    write_status(stage_dir, payload)


def wrong_role(payload: dict[str, object], stage_dir: Path) -> None:
    entries = payload["canonical_executor_outputs"]
    assert isinstance(entries, list)
    entries[0]["role"] = "reviewer"
    write_status(stage_dir, payload)


def duplicate_task_ids(payload: dict[str, object], stage_dir: Path) -> None:
    reviewers = payload["reviewer_outputs"]
    assert isinstance(reviewers, list)
    reviewers[0]["task_id"] = "executor"
    write_status(stage_dir, payload)


def absolute_path(payload: dict[str, object], stage_dir: Path) -> None:
    entries = payload["canonical_executor_outputs"]
    assert isinstance(entries, list)
    entries[0]["path"] = str((stage_dir / "absolute.md").resolve())
    write_status(stage_dir, payload)


def parent_escape(payload: dict[str, object], stage_dir: Path) -> None:
    entries = payload["canonical_executor_outputs"]
    assert isinstance(entries, list)
    entries[0]["path"] = "../escape.md"
    write_status(stage_dir, payload)


def symlink_escape(payload: dict[str, object], stage_dir: Path) -> None:
    outside = stage_dir.parent / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    link = stage_dir / "escape.md"
    symlink_or_skip(link, outside)
    entries = payload["canonical_executor_outputs"]
    assert isinstance(entries, list)
    entries[0]["path"] = "escape.md"
    write_status(stage_dir, payload)


def directory_path(payload: dict[str, object], stage_dir: Path) -> None:
    directory = stage_dir / "directory.md"
    directory.mkdir()
    entries = payload["canonical_executor_outputs"]
    assert isinstance(entries, list)
    entries[0]["path"] = "directory.md"
    write_status(stage_dir, payload)


def missing_file(payload: dict[str, object], stage_dir: Path) -> None:
    entries = payload["canonical_executor_outputs"]
    assert isinstance(entries, list)
    entries[0]["path"] = "missing.md"
    write_status(stage_dir, payload)


def non_markdown_file(payload: dict[str, object], stage_dir: Path) -> None:
    note = stage_dir / "note.txt"
    note.write_text("note", encoding="utf-8")
    entries = payload["canonical_executor_outputs"]
    assert isinstance(entries, list)
    entries[0]["path"] = "note.txt"
    write_status(stage_dir, payload)


INVALID_STATUS_CASES: tuple[tuple[str, StatusMutator], ...] = (
    ("missing-node-id", missing_node_id),
    ("empty-node-id", empty_node_id),
    ("mismatched-node-id", mismatched_node_id),
    ("negative-counter", negative_counter),
    ("boolean-counter", boolean_counter),
    ("missing-counter", missing_counter),
    ("missing-boolean", missing_boolean),
    ("wrong-boolean", wrong_boolean),
    ("wrong-list-type", wrong_list_type),
    ("missing-list", missing_list),
    ("non-object-entry", non_object_entry),
    ("empty-entry-string", empty_entry_string),
    ("wrong-role", wrong_role),
    ("duplicate-task-ids", duplicate_task_ids),
    ("absolute-path", absolute_path),
    ("parent-escape", parent_escape),
    ("symlink-escape", symlink_escape),
    ("directory-path", directory_path),
    ("missing-file", missing_file),
    ("non-markdown-file", non_markdown_file),
)

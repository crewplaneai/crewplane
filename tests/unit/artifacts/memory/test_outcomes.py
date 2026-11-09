from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.artifacts.memory.outcomes import (
    EVENT_LIMIT_BYTES,
    read_invocations,
    read_process_evidence,
    review_qualifiers,
)
from tests.helpers.resume import make_plan
from tests.unit.artifacts.review_loop_status_support import (
    valid_status_payload,
    write_status,
)


def _invocation_record(task_id: str) -> dict[str, object]:
    return {
        "event_type": "invocation_finished",
        "run_id": "run",
        "timestamp": "2026-01-01T00:00:00Z",
        "node_id": "node",
        "task_id": task_id,
        "provider": "alpha",
        "role": "executor",
        "round_num": 1,
    }


def test_invocations_preserve_source_order_and_hash_the_complete_log(tmp_path: Path):
    first, last = _invocation_record("z"), _invocation_record("a")
    records = [
        first,
        {
            "event_type": "workflow_finished",
            "run_id": "run",
            "timestamp": "2026-01-01T00:00:00Z",
        },
        last,
    ]
    payload = ("\r\n".join(json.dumps(item) for item in records) + "\r\n").encode()
    (tmp_path / "logs").mkdir()
    source = tmp_path / "logs/events.ndjson"
    source.write_bytes(payload)

    evidence = read_invocations(tmp_path, "run")

    assert evidence.events == (first, last)
    assert evidence.source_sha256 == hashlib.sha256(payload).hexdigest()
    assert evidence.limitation is None
    assert source.read_bytes() == payload


@pytest.mark.parametrize(
    "invalid_record",
    [
        [],
        {"event_type": "workflow_finished", "run_id": "run", "timestamp": "bad"},
        _invocation_record("task") | {"task_id": ""},
        _invocation_record("task") | {"round_num": True},
    ],
    ids=["non-object", "non-invocation-timestamp", "empty-identity", "boolean-round"],
)
def test_invalid_later_record_discards_all_invocation_evidence(
    tmp_path: Path, invalid_record: object
):
    (tmp_path / "logs").mkdir()
    payload = "\n".join(
        json.dumps(item) for item in [_invocation_record("valid"), invalid_record]
    ).encode()
    (tmp_path / "logs/events.ndjson").write_bytes(payload)

    evidence = read_invocations(tmp_path, "run")

    assert evidence.events == ()
    assert evidence.source_sha256 is None
    assert evidence.limitation is not None
    assert "(ValueError)" in evidence.limitation


@pytest.mark.parametrize("key", ["canonical_executor_outputs", "reviewer_outputs"])
@pytest.mark.parametrize(
    "entries",
    [None, ["bad"], [{"size_bytes": True}], [{"size_bytes": EVENT_LIMIT_BYTES + 1}]],
)
def test_review_descriptor_bounds_are_checked_before_output_resolution(
    tmp_path: Path, key: str, entries: object
):
    node = make_plan(review_loop=True).nodes[0]
    payload = valid_status_payload(node.id)
    payload[key] = entries
    write_status(tmp_path / node.id, payload)

    qualifiers = review_qualifiers(tmp_path, node)

    assert len(qualifiers) == 1
    assert "(ValueError)" in qualifiers[0]


def test_review_metadata_validation_precedes_descriptor_bounds(tmp_path: Path):
    node = make_plan(review_loop=True).nodes[0]
    payload = valid_status_payload("other-node")
    payload["reviewer_outputs"] = None
    write_status(tmp_path / node.id, payload)

    qualifiers = review_qualifiers(tmp_path, node)

    assert len(qualifiers) == 1
    assert "(ReviewLoopStatusError)" in qualifiers[0]


def _process_payload(task_id: str, run_id: str = "run") -> bytes:
    return json.dumps(
        {
            "run_state_schema_version": 1,
            "run_id": run_id,
            "run_key_name": "run-key",
            "node_id": "node",
            "task_id": task_id,
            "provider": "alpha",
            "role": "executor",
            "round_num": 1,
            "attempt": 1,
            "pid": 100,
            "hostname": "test",
            "status": "exited",
            "started_at": "2026-01-01T00:00:00Z",
            "exited_at": "2026-01-01T00:01:00Z",
            "returncode": 0,
        }
    ).encode()


def test_process_receipts_keep_sorted_facts_across_invalid_receipts(tmp_path: Path):
    root = tmp_path / "manifests/provider-processes"
    root.mkdir(parents=True)
    first = _process_payload("first")
    last = _process_payload("last")
    (root / "z.json").write_bytes(last)
    (root / "m-wrong-run.json").write_bytes(_process_payload("wrong", "other"))
    (root / "l-malformed.json").write_bytes(b"invalid JSON")
    (root / "b-directory.json").mkdir()
    (root / "a.json").write_bytes(first)
    (root / "ignored.txt").write_bytes(b"ignored")

    evidence = read_process_evidence(tmp_path, "run")

    assert list(evidence.facts) == ["node"]
    facts = evidence.facts["node"]
    assert len(facts) == 2
    for fact, task, name, payload in zip(
        facts, ("first", "last"), ("a.json", "z.json"), (first, last), strict=True
    ):
        assert fact.startswith(
            f"Provider process {task}, audit None, round 1, attempt 1: exited; return code 0."
        )
        assert (
            f"Receipt manifests/provider-processes/{name}, SHA256 {hashlib.sha256(payload).hexdigest()}."
            in fact
        )
    assert evidence.limitations == tuple(
        f"Provider-process receipt {name} is invalid or unavailable; its process outcome is unknown."
        for name in ("b-directory.json", "l-malformed.json", "m-wrong-run.json")
    )
    assert evidence.qualifications("node") == (*evidence.limitations, *facts)


def test_process_byte_budget_counts_invalid_payloads_and_stops_with_partial_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "manifests/provider-processes"
    root.mkdir(parents=True)
    first = _process_payload("first")
    third = _process_payload("third")
    invalid = b"invalid JSON"
    (root / "a.json").write_bytes(first)
    (root / "b.json").write_bytes(invalid)
    (root / "c.json").write_bytes(third)
    (root / "d.json").mkdir()
    monkeypatch.setattr(
        "crewplane.artifacts.memory.outcomes.EVENT_LIMIT_BYTES",
        len(first) + len(third),
    )

    evidence = read_process_evidence(tmp_path, "run")

    assert len(evidence.facts["node"]) == 1
    assert "Provider process first," in evidence.facts["node"][0]
    assert evidence.limitations == (
        "Provider-process receipt b.json is invalid or unavailable; its process outcome is unknown.",
        "Provider-process receipts exceed total capture byte bound; remaining process outcomes are unknown.",
    )


@pytest.mark.parametrize(
    ("operation", "error_type", "recovered"),
    [
        ("contained_directory", OSError, False),
        ("contained_directory", ValueError, False),
        ("iterdir", OSError, True),
        ("iterdir", ValueError, False),
        ("read_contained_bytes", OSError, True),
        ("read_contained_bytes", ValueError, True),
        ("read_contained_bytes", RuntimeError, False),
    ],
)
def test_process_capture_preserves_operation_specific_exception_boundaries(
    tmp_path: Path, operation: str, error_type: type[Exception], recovered: bool
):
    root = tmp_path / "manifests/provider-processes"
    root.mkdir(parents=True)
    (root / "process.json").write_bytes(_process_payload("task"))
    target = (
        "pathlib.Path.iterdir"
        if operation == "iterdir"
        else f"crewplane.artifacts.memory.outcomes.{operation}"
    )

    with patch(target, side_effect=error_type("capture failure")):
        if not recovered:
            with pytest.raises(error_type, match="capture failure"):
                read_process_evidence(tmp_path, "run")
            return
        evidence = read_process_evidence(tmp_path, "run")

    assert evidence.facts == {}
    assert len(evidence.limitations) == 1
    expected = "inventory is unreadable" if operation == "iterdir" else "process.json"
    assert expected in evidence.limitations[0]

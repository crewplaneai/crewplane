from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from pathlib import Path

from crewplane.architecture.contracts import JsonObject
from crewplane.architecture.contracts.execution_event import EventType
from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.architecture.safe_files import contained_directory
from crewplane.artifacts.naming import safe_stage_name
from crewplane.artifacts.results.review_loop_status import (
    ReviewLoopStatusError,
    resolve_status_outputs,
    task_specs_for_producers,
    validate_review_loop_output_set,
    validate_status_metadata,
)
from crewplane.core.execution_state import NodeState, RunManifest
from crewplane.core.preflight.compiled_records import PreflightExecutionNode
from crewplane.core.provider_process_state import ProviderProcessState

STRUCTURED_LIMIT_BYTES = 64 * 1024
EVENT_LIMIT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class InvocationEvidence:
    """Invocation capture accepted or rejected as a whole by ``read_invocations``.

    Attributes:
        events: Validated invocation records in source order, without lifecycle
            pairing; empty when capture failed or no invocations were logged.
        source_sha256: Digest of the complete log, or None when capture failed.
        limitation: Capture failure qualification, or None for a valid source.
    """

    events: tuple[JsonObject, ...]
    source_sha256: str | None
    limitation: str | None


def read_invocations(stages_dir: Path, run_id: str) -> InvocationEvidence:
    """Read the run's contained ``logs/events.ndjson`` with an 8 MiB bound.

    Validate every record, including non-invocation events, before returning
    invocation records in source order and the complete source's SHA256.
    OSError, ValueError, and UnicodeError discard all records and the digest,
    returning a limitation instead. Other exceptions propagate.
    """
    try:
        payload = read_contained_bytes(
            stages_dir, "logs/events.ndjson", EVENT_LIMIT_BYTES
        )
        records: list[JsonObject] = []
        for line in payload.decode("utf-8").splitlines():
            record = json.loads(line)
            _validate_event_record(record, run_id)
            if not record["event_type"].startswith("invocation_"):
                continue
            _validate_invocation_identity(record)
            records.append(record)
        return InvocationEvidence(
            tuple(records), hashlib.sha256(payload).hexdigest(), None
        )
    except (OSError, ValueError, UnicodeError) as exc:
        return InvocationEvidence(
            (),
            None,
            f"Invocation lifecycle evidence is missing, incomplete, unsafe, or exceeds its bound ({type(exc).__name__}); invocation outcomes are unknown.",
        )


def _validate_event_record(record: object, run_id: str) -> None:
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("event_type"), str)
        or record.get("run_id") != run_id
        or not isinstance(record.get("timestamp"), str)
    ):
        raise ValueError("incomplete or mismatched structured event")
    EventType(record["event_type"])
    datetime.fromisoformat(record["timestamp"])


def _validate_invocation_identity(record: JsonObject) -> None:
    if not all(
        isinstance(record.get(key), str) and record[key]
        for key in ("node_id", "task_id", "provider", "role")
    ):
        raise ValueError("incomplete invocation identity")
    if (
        record["role"] not in {"executor", "reviewer"}
        or type(record.get("round_num")) is not int
    ):
        raise ValueError("invalid invocation role or round")


def node_invocation_qualifiers(
    node_id: str, evidence: InvocationEvidence
) -> tuple[str, ...]:
    """Return a node's invocation uncertainty or lifecycle counts without I/O.

    A capture limitation takes precedence over retained events; absent node
    events produce an unknown-outcome qualification. Otherwise return terminal
    outcome counts followed by start/terminal counts, without pairing events or
    claiming that tests passed.
    """
    events = tuple(item for item in evidence.events if item.get("node_id") == node_id)
    if evidence.limitation is not None:
        return (evidence.limitation,)
    if not events:
        return (
            "No current-run invocation evidence was retained; invocation outcomes are unknown.",
        )
    failed = sum(item.get("event_type") == "invocation_failed" for item in events)
    finished = sum(item.get("event_type") == "invocation_finished" for item in events)
    started = sum(item.get("event_type") == "invocation_started" for item in events)
    return (
        f"Framework recorded {finished} successful and {failed} failed invocations for this node, including partial failures; this is not proof that tests passed.",
        f"Invocation lifecycle completeness: {started} starts and {finished + failed} terminal events; unmatched events are unknown outcomes.",
    )


def has_new_execution(
    node: PreflightExecutionNode,
    state: NodeState,
    manifest: RunManifest,
    evidence: InvocationEvidence,
) -> bool:
    """Return whether invocation evidence or resume metadata indicates new work.

    A finished or failed invocation for the node returns True even for resumed
    work. Without such an event, including missing or invalid capture, return
    True only when the state has no resume origin and the manifest records
    neither node reuse nor review-checkpoint reuse for this node. No I/O occurs.
    """
    if any(
        item.get("node_id") == node.id
        and item.get("event_type") in {"invocation_finished", "invocation_failed"}
        for item in evidence.events
    ):
        return True
    return (
        state.resume_origin is None
        and node.id not in manifest.resumed_nodes
        and not any(
            item.node_id == node.id for item in manifest.resumed_review_checkpoints
        )
    )


def review_qualifiers(
    stages_dir: Path, node: PreflightExecutionNode
) -> tuple[str, ...]:
    """Return review consensus and selected-artifact lineage qualifications.

    Without a reviewer, return a no-review qualification without I/O. Otherwise
    validate the contained review status (64 KiB maximum) and its selected
    artifacts (8 MiB each), including producer identities, sizes, and hashes.
    Return consensus context followed by executor and then reviewer identities;
    agreement does not establish that tests passed. OSError, ValueError, and
    ReviewLoopStatusError become an unknown-evidence qualification; other
    exceptions propagate.
    """
    if not any(provider.role == "reviewer" for provider in node.provider_records):
        return ("No review consensus was requested for this node.",)
    relative = f"{safe_stage_name(node.id)}/review-state/review-loop-status.json"
    try:
        payload = json.loads(
            read_contained_bytes(stages_dir, relative, STRUCTURED_LIMIT_BYTES)
        )
        if not isinstance(payload, dict):
            raise ValueError("review status is not an object")
        validate_status_metadata(payload, node.id)
        _validate_review_output_descriptors(payload)
        stage_dir = stages_dir / safe_stage_name(node.id)
        canonical, reviewers = resolve_status_outputs(
            payload, stage_dir, EVENT_LIMIT_BYTES
        )
        validate_review_loop_output_set(
            payload,
            canonical,
            reviewers,
            task_specs_for_producers(node.provider_records),
        )
        identities = tuple(
            f"Selected {item.role} artifact: {item.task_id}; audit {item.audit_round_num}; round {item.round_num}; {item.relative_path}; SHA256 {item.sha256}."
            for item in (*canonical, *reviewers)
        )
        return (
            f"Review consensus reached: {payload['consensus_reached']}; continued after exhaustion: {payload['continued_after_consensus_exhaustion']}; stop reason: {payload.get('stop_reason', 'not recorded')}. Review agreement is not proof that tests passed.",
            *identities,
        )
    except (OSError, ValueError, ReviewLoopStatusError) as exc:
        return (
            f"Review consensus and selected-artifact lineage are unknown: retained review evidence is unavailable or invalid ({type(exc).__name__}). Do not infer approval from node success.",
        )


def _validate_review_output_descriptors(payload: dict[str, object]) -> None:
    for key in ("canonical_executor_outputs", "reviewer_outputs"):
        entries = payload.get(key)
        if not isinstance(entries, list) or any(
            not isinstance(item, dict)
            or type(item.get("size_bytes")) is not int
            or not 0 <= item["size_bytes"] <= EVENT_LIMIT_BYTES
            for item in entries
        ):
            raise ValueError("review output descriptor exceeds protected read bounds")


@dataclass(frozen=True)
class ProcessEvidence:
    """Process receipt facts retained even when other receipts cannot be captured.

    Attributes:
        facts: Per-node receipt qualifications in sorted receipt-path order,
            including process status, return code, source path, and digest.
        limitations: Capture-wide uncertainty retained alongside valid facts;
            process exits do not establish that tests passed.
    """

    facts: dict[str, tuple[str, ...]]
    limitations: tuple[str, ...] = ()

    def qualifications(self, node_id: str) -> tuple[str, ...]:
        """Return capture limitations followed by the node's facts without I/O.

        If the node has no facts, append an unknown-process-outcome qualification
        after any limitations. Preserve the order of limitations and facts.
        """
        return (
            *self.limitations,
            *self.facts.get(
                node_id,
                (
                    "No provider-process receipt for this node; process outcome is unknown.",
                ),
            ),
        )


def read_process_evidence(stages_dir: Path, run_id: str) -> ProcessEvidence:
    """Capture current-run provider-process facts, retaining partial evidence.

    Retain at most 1,025 directory entries; reject inventories exceeding 1,024.
    Directory enumeration itself is unbounded: ``Path.iterdir()`` can eagerly
    list the entire directory before the cap on retained entries applies.
    Read only JSON receipts in sorted path order, with a 64 KiB per-file bound.
    Check the 8 MiB cumulative payload budget after each successful read; the
    receipt crossing it and all remaining receipts are left unprocessed.

    Missing directories, inventory OSError, and oversized inventories return
    empty facts with a limitation. Per-receipt OSError or ValueError, including
    schema and run-identity failures, add limitations while preserving valid
    facts. Reaching the cumulative budget preserves earlier facts as well.
    Other exceptions propagate.

    Raises:
        OSError: Initial receipt-directory resolution fails other than absence.
        ValueError: Initial receipt-directory safety validation fails.
    """
    paths, inventory_limitations = _read_process_receipt_inventory(stages_dir)
    facts: dict[str, list[str]] = {}
    limitations = list(inventory_limitations)
    examined = 0
    for path in paths:
        if path.suffix != ".json":
            continue
        try:
            payload = read_contained_bytes(
                stages_dir,
                path.relative_to(stages_dir).as_posix(),
                STRUCTURED_LIMIT_BYTES,
            )
            examined += len(payload)
            if examined > EVENT_LIMIT_BYTES:
                limitations.append(
                    "Provider-process receipts exceed total capture byte bound; remaining process outcomes are unknown."
                )
                break
            state = _validate_process_receipt(payload, run_id)
            facts.setdefault(state.node_id, []).append(
                _process_receipt_qualification(
                    state, path.relative_to(stages_dir).as_posix(), payload
                )
            )
        except (OSError, ValueError):
            limitations.append(
                f"Provider-process receipt {path.name} is invalid or unavailable; its process outcome is unknown."
            )
    return ProcessEvidence(
        {key: tuple(value) for key, value in facts.items()}, tuple(limitations)
    )


def _read_process_receipt_inventory(
    stages_dir: Path,
) -> tuple[list[Path], tuple[str, ...]]:
    root = contained_directory(stages_dir, "manifests/provider-processes")
    if root is None:
        return [], (
            "Provider-process receipts are unavailable; no provider exit-code claim is established.",
        )
    try:
        paths = sorted(islice(root.iterdir(), 1025))
    except OSError:
        return [], (
            "Provider-process receipt inventory is unreadable; process outcomes are unknown.",
        )
    if len(paths) > 1024:
        return [], (
            "Provider-process receipt inventory exceeds capture bound; process outcomes are unknown.",
        )
    return paths, ()


def _validate_process_receipt(payload: bytes, run_id: str) -> ProviderProcessState:
    state = ProviderProcessState.model_validate_json(payload)
    if state.run_id != run_id:
        raise ValueError("process receipt run identity mismatch")
    return state


def _process_receipt_qualification(
    state: ProviderProcessState, relative_path: str, payload: bytes
) -> str:
    return f"Provider process {state.task_id}, audit {state.audit_round_num}, round {state.round_num}, attempt {state.attempt}: {state.status}; return code {state.returncode}. Receipt {relative_path}, SHA256 {hashlib.sha256(payload).hexdigest()}. This is process evidence, not proof that tests passed."

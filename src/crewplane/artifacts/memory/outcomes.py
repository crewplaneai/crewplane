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
    events: tuple[JsonObject, ...]
    source_sha256: str | None
    limitation: str | None


def read_invocations(stages_dir: Path, run_id: str) -> InvocationEvidence:
    try:
        payload = read_contained_bytes(
            stages_dir, "logs/events.ndjson", EVENT_LIMIT_BYTES
        )
        records: list[JsonObject] = []
        for line in payload.decode("utf-8").splitlines():
            record = json.loads(line)
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("event_type"), str)
                or record.get("run_id") != run_id
                or not isinstance(record.get("timestamp"), str)
            ):
                raise ValueError("incomplete or mismatched structured event")
            EventType(record["event_type"])
            datetime.fromisoformat(record["timestamp"])
            if record["event_type"].startswith("invocation_"):
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


def node_invocation_qualifiers(
    node_id: str, evidence: InvocationEvidence
) -> tuple[str, ...]:
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
        for key in ("canonical_executor_outputs", "reviewer_outputs"):
            entries = payload.get(key)
            if not isinstance(entries, list) or any(
                not isinstance(item, dict)
                or type(item.get("size_bytes")) is not int
                or not 0 <= item["size_bytes"] <= EVENT_LIMIT_BYTES
                for item in entries
            ):
                raise ValueError(
                    "review output descriptor exceeds protected read bounds"
                )
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


@dataclass(frozen=True)
class ProcessEvidence:
    facts: dict[str, tuple[str, ...]]
    limitations: tuple[str, ...] = ()

    def qualifications(self, node_id: str) -> tuple[str, ...]:
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
    root = contained_directory(stages_dir, "manifests/provider-processes")
    if root is None:
        return ProcessEvidence(
            {},
            (
                "Provider-process receipts are unavailable; no provider exit-code claim is established.",
            ),
        )
    facts: dict[str, list[str]] = {}
    limitations: list[str] = []
    try:
        paths = sorted(islice(root.iterdir(), 1025))
    except OSError:
        return ProcessEvidence(
            {},
            (
                "Provider-process receipt inventory is unreadable; process outcomes are unknown.",
            ),
        )
    if len(paths) > 1024:
        return ProcessEvidence(
            {},
            (
                "Provider-process receipt inventory exceeds capture bound; process outcomes are unknown.",
            ),
        )
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
            state = ProviderProcessState.model_validate_json(payload)
            if state.run_id != run_id:
                raise ValueError("process receipt run identity mismatch")
            facts.setdefault(state.node_id, []).append(
                f"Provider process {state.task_id}, audit {state.audit_round_num}, round {state.round_num}, attempt {state.attempt}: {state.status}; return code {state.returncode}. Receipt {path.relative_to(stages_dir).as_posix()}, SHA256 {hashlib.sha256(payload).hexdigest()}. This is process evidence, not proof that tests passed."
            )
        except (OSError, ValueError):
            limitations.append(
                f"Provider-process receipt {path.name} is invalid or unavailable; its process outcome is unknown."
            )
    return ProcessEvidence(
        {key: tuple(value) for key, value in facts.items()}, tuple(limitations)
    )

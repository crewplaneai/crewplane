from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypedDict

from crewplane.architecture.ports.artifacts import StageTaskSpec
from crewplane.architecture.safe_files import contained_regular_file
from crewplane.core.workflow.keywords import ProviderRole

REVIEW_LOOP_STATUS_RELATIVE_PATH = Path("review-state") / "review-loop-status.json"
REQUIRED_COUNTER_FIELDS = (
    "executed_audit_rounds",
    "attempted_local_round_num",
    "final_local_round_num",
    "invalid_candidate_round_count",
    "no_progress_round_count",
    "artifact_drift_warning_count",
)
REQUIRED_BOOLEAN_FIELDS = (
    "consensus_reached",
    "continued_after_consensus_exhaustion",
)


class ReviewLoopStatusOutputEntry(TypedDict):
    task_id: str
    provider: str
    role: ProviderRole
    path: str
    sha256: str
    size_bytes: int
    audit_round_num: int | None
    round_num: int


class ReviewLoopStatusPayload(TypedDict):
    node_id: str
    executed_audit_rounds: int
    attempted_local_round_num: int
    final_local_round_num: int
    consensus_reached: bool
    continued_after_consensus_exhaustion: bool
    invalid_candidate_round_count: int
    no_progress_round_count: int
    artifact_drift_warning_count: int
    canonical_executor_outputs: list[ReviewLoopStatusOutputEntry]
    reviewer_outputs: list[ReviewLoopStatusOutputEntry]


class ReviewLoopStatusError(RuntimeError):
    """Raised when a review-loop status artifact is present but invalid."""


class _TaskProducer(Protocol):
    task_id: str
    provider: str
    role: ProviderRole


def task_specs_for_producers(
    producers: Iterable[_TaskProducer],
) -> tuple[StageTaskSpec, ...]:
    """Build the producer identity contract used to validate review evidence."""

    return tuple(
        StageTaskSpec(
            task_id=producer.task_id,
            role=producer.role,
            provider=producer.provider,
        )
        for producer in producers
    )


@dataclass(frozen=True)
class ReviewLoopStatusEntry:
    task_id: str
    provider: str
    role: ProviderRole
    relative_path: str
    output_file: Path
    sha256: str
    size_bytes: int
    audit_round_num: int | None
    round_num: int


@dataclass(frozen=True)
class ResolvedReviewLoopStatus:
    canonical_executor_outputs: tuple[ReviewLoopStatusEntry, ...]
    reviewer_outputs: tuple[ReviewLoopStatusEntry, ...]

    @property
    def selected_output_files(self) -> dict[str, Path]:
        return {
            entry.task_id: entry.output_file
            for entry in (*self.canonical_executor_outputs, *self.reviewer_outputs)
        }


def resolve_review_loop_status(
    stage_name: str,
    stage_dir: Path,
    task_specs: tuple[StageTaskSpec, ...] = (),
) -> ResolvedReviewLoopStatus | None:
    status_path = locate_review_loop_status(stage_dir)
    if status_path is None:
        return None

    payload = load_status_payload(status_path)
    validate_status_metadata(payload, stage_name)
    canonical_outputs, reviewer_outputs = resolve_status_outputs(payload, stage_dir)
    validate_review_loop_output_set(
        payload,
        canonical_outputs,
        reviewer_outputs,
        task_specs,
    )
    return ResolvedReviewLoopStatus(
        canonical_executor_outputs=canonical_outputs,
        reviewer_outputs=reviewer_outputs,
    )


def review_loop_status_path(stage_dir: Path) -> Path:
    return stage_dir / REVIEW_LOOP_STATUS_RELATIVE_PATH


def locate_review_loop_status(stage_dir: Path) -> Path | None:
    status_candidate = review_loop_status_path(stage_dir)
    status_path = contained_regular_file(
        stage_dir,
        REVIEW_LOOP_STATUS_RELATIVE_PATH.as_posix(),
    )
    if status_path is None:
        try:
            status_candidate.lstat()
        except FileNotFoundError:
            try:
                status_candidate.parent.lstat()
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise status_error(
                    "review-loop status directory could not be inspected"
                ) from exc
            if status_candidate.parent.is_symlink():
                raise status_error(
                    "review-loop status directory must not be a symlink"
                ) from None
            return None
        except OSError as exc:
            raise status_error(
                "review-loop status path could not be inspected"
            ) from exc
        raise status_error("review-loop status path must be a safe regular file")
    return status_path


def validate_status_metadata(
    payload: dict[str, object],
    stage_name: str,
) -> None:
    validate_status_identity(payload, stage_name)
    validate_status_counters(payload)
    validate_status_booleans(payload)


def resolve_status_outputs(
    payload: dict[str, object],
    stage_dir: Path,
) -> tuple[
    tuple[ReviewLoopStatusEntry, ...],
    tuple[ReviewLoopStatusEntry, ...],
]:
    canonical_outputs = parse_status_entries(
        payload,
        "canonical_executor_outputs",
        ProviderRole.EXECUTOR,
        stage_dir,
    )
    reviewer_outputs = parse_status_entries(
        payload,
        "reviewer_outputs",
        ProviderRole.REVIEWER,
        stage_dir,
    )
    return canonical_outputs, reviewer_outputs


def validate_review_loop_output_set(
    payload: dict[str, object],
    canonical_outputs: tuple[ReviewLoopStatusEntry, ...],
    reviewer_outputs: tuple[ReviewLoopStatusEntry, ...],
    task_specs: tuple[StageTaskSpec, ...],
) -> None:
    validate_unique_task_ids(canonical_outputs, reviewer_outputs)
    validate_expected_task_producers(
        canonical_outputs,
        reviewer_outputs,
        task_specs,
    )
    validate_round_attribution(payload, canonical_outputs, reviewer_outputs)


def load_status_payload(status_path: Path) -> dict[str, object]:
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise status_error(f"malformed JSON in '{status_path}'") from exc
    if not isinstance(payload, dict):
        raise status_error("payload must be a JSON object")
    return payload


def validate_status_identity(payload: dict[str, object], stage_name: str) -> None:
    node_id = payload.get("node_id")
    if not isinstance(node_id, str) or not node_id.strip():
        raise status_error("node_id must be a non-empty string")
    if node_id != stage_name:
        raise status_error(
            f"node_id '{node_id}' does not match finalized stage '{stage_name}'"
        )


def validate_status_counters(payload: dict[str, object]) -> None:
    for field_name in REQUIRED_COUNTER_FIELDS:
        value = payload.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise status_error(f"{field_name} must be a non-negative integer")
    final_round = payload["final_local_round_num"]
    attempted_round = payload["attempted_local_round_num"]
    if (
        isinstance(final_round, int)
        and isinstance(attempted_round, int)
        and final_round > attempted_round
    ):
        raise status_error(
            "final_local_round_num cannot exceed attempted_local_round_num"
        )


def validate_status_booleans(payload: dict[str, object]) -> None:
    for field_name in REQUIRED_BOOLEAN_FIELDS:
        if not isinstance(payload.get(field_name), bool):
            raise status_error(f"{field_name} must be a boolean")


def parse_status_entries(
    payload: dict[str, object],
    field_name: str,
    expected_role: ProviderRole,
    stage_dir: Path,
) -> tuple[ReviewLoopStatusEntry, ...]:
    raw_entries = payload.get(field_name)
    if not isinstance(raw_entries, list):
        raise status_error(f"{field_name} must be a list")
    return tuple(
        parse_status_entry(raw_entry, field_name, index, expected_role, stage_dir)
        for index, raw_entry in enumerate(raw_entries)
    )


def parse_status_entry(
    raw_entry: object,
    field_name: str,
    index: int,
    expected_role: ProviderRole,
    stage_dir: Path,
) -> ReviewLoopStatusEntry:
    if not isinstance(raw_entry, dict):
        raise status_error(f"{field_name}[{index}] must be an object")
    task_id = non_empty_entry_string(raw_entry, field_name, index, "task_id")
    provider = non_empty_entry_string(raw_entry, field_name, index, "provider")
    raw_role = non_empty_entry_string(raw_entry, field_name, index, "role")
    relative_path = non_empty_entry_string(raw_entry, field_name, index, "path")
    sha256 = non_empty_entry_string(raw_entry, field_name, index, "sha256")
    size_bytes = non_negative_entry_int(raw_entry, field_name, index, "size_bytes")
    round_num = positive_entry_int(raw_entry, field_name, index, "round_num")
    audit_round_num = optional_positive_entry_int(
        raw_entry, field_name, index, "audit_round_num"
    )
    if raw_role != expected_role.value:
        raise status_error(
            f"{field_name}[{index}].role must be '{expected_role.value}', "
            f"got '{raw_role}'"
        )
    validate_entry_locator_attribution(
        relative_path,
        audit_round_num,
        round_num,
        field_name,
        index,
    )
    output_file = resolve_status_output_file(
        stage_dir, relative_path, field_name, index
    )
    payload = output_file.read_bytes()
    if len(payload) != size_bytes or hashlib.sha256(payload).hexdigest() != sha256:
        raise status_error(f"{field_name}[{index}] bytes do not match its descriptor")
    return ReviewLoopStatusEntry(
        task_id=task_id,
        provider=provider,
        role=expected_role,
        relative_path=relative_path,
        output_file=output_file,
        sha256=sha256,
        size_bytes=size_bytes,
        audit_round_num=audit_round_num,
        round_num=round_num,
    )


def non_negative_entry_int(
    raw_entry: dict[object, object],
    field_name: str,
    index: int,
    key: str,
) -> int:
    value = raw_entry.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise status_error(f"{field_name}[{index}].{key} must be non-negative")
    return value


def positive_entry_int(
    raw_entry: dict[object, object],
    field_name: str,
    index: int,
    key: str,
) -> int:
    value = non_negative_entry_int(raw_entry, field_name, index, key)
    if value == 0:
        raise status_error(f"{field_name}[{index}].{key} must be positive")
    return value


def optional_positive_entry_int(
    raw_entry: dict[object, object],
    field_name: str,
    index: int,
    key: str,
) -> int | None:
    if raw_entry.get(key) is None:
        return None
    return positive_entry_int(raw_entry, field_name, index, key)


def validate_entry_locator_attribution(
    relative_path: str,
    audit_round_num: int | None,
    round_num: int,
    field_name: str,
    index: int,
) -> None:
    path = Path(relative_path)
    expected_parent = (
        Path(".")
        if audit_round_num is None
        else Path(f"review-audit-round-{audit_round_num}")
    )
    if path.parent != expected_parent or not path.stem.endswith(f"_round{round_num}"):
        raise status_error(
            f"{field_name}[{index}].path conflicts with its audit/round attribution"
        )


def non_empty_entry_string(
    raw_entry: dict[object, object],
    field_name: str,
    index: int,
    key: str,
) -> str:
    value = raw_entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise status_error(f"{field_name}[{index}].{key} must be a non-empty string")
    return value


def resolve_status_output_file(
    stage_dir: Path,
    relative_path: str,
    field_name: str,
    index: int,
) -> Path:
    if Path(relative_path).is_absolute():
        raise status_error(f"{field_name}[{index}].path must be relative")
    if not relative_path.endswith(".md"):
        raise status_error(f"{field_name}[{index}].path must point to a .md file")
    output_file = contained_regular_file(stage_dir, relative_path)
    if output_file is None:
        raise status_error(
            f"{field_name}[{index}].path must point to a safe regular file"
        )
    return output_file


def validate_unique_task_ids(
    canonical_outputs: tuple[ReviewLoopStatusEntry, ...],
    reviewer_outputs: tuple[ReviewLoopStatusEntry, ...],
) -> None:
    seen: set[str] = set()
    for entry in (*canonical_outputs, *reviewer_outputs):
        if entry.task_id in seen:
            raise status_error(f"duplicate task_id '{entry.task_id}'")
        seen.add(entry.task_id)


def validate_expected_task_producers(
    canonical_outputs: tuple[ReviewLoopStatusEntry, ...],
    reviewer_outputs: tuple[ReviewLoopStatusEntry, ...],
    task_specs: tuple[StageTaskSpec, ...],
) -> None:
    if not task_specs:
        return
    expected = {task.task_id: task for task in task_specs}
    for entry in (*canonical_outputs, *reviewer_outputs):
        task = expected.get(entry.task_id)
        if task is None:
            raise status_error(f"unknown task_id '{entry.task_id}'")
        if task.role != entry.role:
            raise status_error(
                f"task_id '{entry.task_id}' has an unexpected producer role"
            )
        if task.provider is not None and task.provider != entry.provider:
            raise status_error(
                f"task_id '{entry.task_id}' has an unexpected producer provider"
            )


def validate_round_attribution(
    payload: dict[str, object],
    canonical_outputs: tuple[ReviewLoopStatusEntry, ...],
    reviewer_outputs: tuple[ReviewLoopStatusEntry, ...],
) -> None:
    entries = (*canonical_outputs, *reviewer_outputs)
    attributions = {(entry.audit_round_num, entry.round_num) for entry in entries}
    if len(attributions) > 1:
        raise status_error("selected outputs must belong to one audit and local round")
    if entries and entries[0].round_num != payload["final_local_round_num"]:
        raise status_error("selected output round does not match final_local_round_num")


def status_error(message: str) -> ReviewLoopStatusError:
    return ReviewLoopStatusError(f"Invalid review-loop status artifact: {message}")

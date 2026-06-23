from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from crewplane.core.workflow.keywords import ProviderRole

REVIEW_LOOP_STATUS_RELATIVE_PATH = Path("review-state") / "review-loop-status.json"
REQUIRED_COUNTER_FIELDS = (
    "executed_audit_rounds",
    "final_local_round_num",
    "invalid_candidate_round_count",
    "no_progress_round_count",
    "artifact_drift_warning_count",
)
REQUIRED_BOOLEAN_FIELDS = (
    "consensus_reached",
    "continued_after_consensus_exhaustion",
)


class ReviewLoopStatusError(RuntimeError):
    """Raised when a review-loop status artifact is present but invalid."""


@dataclass(frozen=True)
class ReviewLoopStatusEntry:
    task_id: str
    provider: str
    role: ProviderRole
    relative_path: str
    output_file: Path


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
) -> ResolvedReviewLoopStatus | None:
    status_path = stage_dir / REVIEW_LOOP_STATUS_RELATIVE_PATH
    if not status_path.exists():
        return None
    payload = load_status_payload(status_path)
    validate_status_identity(payload, stage_name)
    validate_status_counters(payload)
    validate_status_booleans(payload)
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
    validate_unique_task_ids(canonical_outputs, reviewer_outputs)
    return ResolvedReviewLoopStatus(
        canonical_executor_outputs=canonical_outputs,
        reviewer_outputs=reviewer_outputs,
    )


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
    if raw_role != expected_role.value:
        raise status_error(
            f"{field_name}[{index}].role must be '{expected_role.value}', "
            f"got '{raw_role}'"
        )
    output_file = resolve_status_output_file(
        stage_dir, relative_path, field_name, index
    )
    return ReviewLoopStatusEntry(
        task_id=task_id,
        provider=provider,
        role=expected_role,
        relative_path=relative_path,
        output_file=output_file,
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
    candidate_path = Path(relative_path)
    if candidate_path.is_absolute():
        raise status_error(f"{field_name}[{index}].path must be relative")
    stage_root = stage_dir.resolve()
    output_file = (stage_dir / candidate_path).resolve()
    try:
        output_file.relative_to(stage_root)
    except ValueError as exc:
        raise status_error(
            f"{field_name}[{index}].path escapes the stage directory"
        ) from exc
    if output_file.suffix != ".md":
        raise status_error(f"{field_name}[{index}].path must point to a .md file")
    if not output_file.exists():
        raise status_error(f"{field_name}[{index}].path points to a missing file")
    if not output_file.is_file():
        raise status_error(f"{field_name}[{index}].path must point to a file")
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


def status_error(message: str) -> ReviewLoopStatusError:
    return ReviewLoopStatusError(f"Invalid review-loop status artifact: {message}")

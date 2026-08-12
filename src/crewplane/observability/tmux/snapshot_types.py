from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal, NotRequired, TypedDict, TypeGuard

from crewplane.observability.events.types import InvocationStatus


class SelectedInvocationSnapshot(TypedDict):
    schema_version: int
    workflow_name: str
    run_id: str
    dashboard_generation: int
    selection_generation: int
    requested_selected_index: int
    resolved_selected_index: int
    node_count: int
    node_id: str | None
    written_at: float
    task_id: NotRequired[str]
    provider: NotRequired[str]
    role: NotRequired[str]
    model: NotRequired[str | None]
    audit_round_num: NotRequired[int | None]
    round_num: NotRequired[int | None]
    invocation_status: NotRequired[InvocationStatus]
    output_file: NotRequired[str | None]
    log_file: NotRequired[str | None]
    log_presentation_format: NotRequired[str]
    log_presentation_profile: NotRequired[str]


InspectView = Literal["raw", "formatted"]


class InspectInvocationSnapshot(SelectedInvocationSnapshot):
    inspect_view: InspectView
    line_budget: int
    created_at: float


InvocationSnapshot = SelectedInvocationSnapshot | InspectInvocationSnapshot


_REQUIRED_STRING_FIELDS = ("workflow_name", "run_id")
_REQUIRED_INTEGER_FIELDS = (
    "schema_version",
    "dashboard_generation",
    "selection_generation",
    "requested_selected_index",
    "resolved_selected_index",
    "node_count",
)
_OPTIONAL_STRING_FIELDS = (
    "task_id",
    "provider",
    "role",
    "log_presentation_format",
    "log_presentation_profile",
)
_OPTIONAL_NULLABLE_STRING_FIELDS = ("model", "output_file", "log_file")
_OPTIONAL_NULLABLE_INTEGER_FIELDS = ("audit_round_num", "round_num")
_INVOCATION_STATUSES = frozenset({"pending", "running", "succeeded", "failed"})
_INSPECT_VIEWS = frozenset({"raw", "formatted"})


def is_selected_invocation_snapshot(
    value: object,
) -> TypeGuard[SelectedInvocationSnapshot]:
    if not isinstance(value, Mapping):
        return False
    if not all(isinstance(key, str) for key in value):
        return False

    snapshot: Mapping[str, object] = value
    return (
        _matches_required_fields(snapshot, _REQUIRED_STRING_FIELDS, _is_string)
        and _matches_required_fields(snapshot, _REQUIRED_INTEGER_FIELDS, _is_integer)
        and _field_is(snapshot, "node_id", _is_nullable_string)
        and _field_is(snapshot, "written_at", _is_float)
        and _matches_optional_fields(snapshot, _OPTIONAL_STRING_FIELDS, _is_string)
        and _matches_optional_fields(
            snapshot,
            _OPTIONAL_NULLABLE_STRING_FIELDS,
            _is_nullable_string,
        )
        and _matches_optional_fields(
            snapshot,
            _OPTIONAL_NULLABLE_INTEGER_FIELDS,
            _is_nullable_integer,
        )
        and _field_is_optional(snapshot, "invocation_status", _is_invocation_status)
    )


def is_inspect_invocation_snapshot(
    value: object,
) -> TypeGuard[InspectInvocationSnapshot]:
    if not is_selected_invocation_snapshot(value):
        return False

    snapshot: Mapping[str, object] = value
    return (
        _field_is(snapshot, "inspect_view", _is_inspect_view)
        and _field_is(snapshot, "line_budget", _is_integer)
        and _field_is(snapshot, "created_at", _is_float)
    )


def _matches_required_fields(
    snapshot: Mapping[str, object],
    field_names: tuple[str, ...],
    predicate: Callable[[object], bool],
) -> bool:
    return all(
        field in snapshot and predicate(snapshot[field]) for field in field_names
    )


def _matches_optional_fields(
    snapshot: Mapping[str, object],
    field_names: tuple[str, ...],
    predicate: Callable[[object], bool],
) -> bool:
    return all(
        field not in snapshot or predicate(snapshot[field]) for field in field_names
    )


def _field_is(
    snapshot: Mapping[str, object],
    field_name: str,
    predicate: Callable[[object], bool],
) -> bool:
    return field_name in snapshot and predicate(snapshot[field_name])


def _field_is_optional(
    snapshot: Mapping[str, object],
    field_name: str,
    predicate: Callable[[object], bool],
) -> bool:
    return field_name not in snapshot or predicate(snapshot[field_name])


def _is_string(value: object) -> bool:
    return isinstance(value, str)


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_float(value: object) -> bool:
    return isinstance(value, float)


def _is_nullable_string(value: object) -> bool:
    return value is None or _is_string(value)


def _is_nullable_integer(value: object) -> bool:
    return value is None or _is_integer(value)


def _is_invocation_status(value: object) -> bool:
    return isinstance(value, str) and value in _INVOCATION_STATUSES


def _is_inspect_view(value: object) -> bool:
    return isinstance(value, str) and value in _INSPECT_VIEWS


def snapshot_string(
    snapshot: Mapping[str, object] | None,
    key: str,
) -> str | None:
    if snapshot is None:
        return None
    value = snapshot.get(key)
    return value if isinstance(value, str) else None


def require_snapshot_string(
    snapshot: Mapping[str, object],
    key: str,
) -> str:
    value = snapshot_string(snapshot, key)
    if not value:
        raise ValueError(f"snapshot missing {key}")
    return value

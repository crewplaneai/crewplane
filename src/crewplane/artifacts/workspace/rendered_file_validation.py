from __future__ import annotations

from pathlib import Path

from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
    WorkspaceFileLocator,
)

from ..run_history import RunHistoryRecord
from ..safe_files import contained_regular_file
from .bundle_validation import workspace_blob_descriptor_matches
from .state.fields import int_field, nullable_int_field

_SUPPORTED_RENDERED_FILE_MODES = (
    "100644",  # Git mode for a regular, non-executable blob.
    "100755",  # Git mode for a regular executable blob.
)


def provider_rendered_workspace_files_match(
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payload: dict[str, object],
    source: RunHistoryRecord | None = None,
) -> bool:
    expected = _expected_rendered_locators(plan, node, payload)
    rendered = payload.get("rendered_workspace_files")
    if not expected:
        return rendered is None or rendered == []
    if not isinstance(rendered, list) or len(rendered) != len(expected):
        return False
    descriptors = [_mapping(item) for item in rendered]
    if any(not descriptor for descriptor in descriptors):
        return False
    descriptors_by_occurrence = _descriptors_by_occurrence(descriptors)
    if descriptors_by_occurrence is None:
        return False
    return all(
        _rendered_descriptor_matches_locator(
            payload,
            descriptors_by_occurrence,
            locator,
            plan,
            source,
        )
        for locator in expected
    )


def _expected_rendered_locators(
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payload: dict[str, object],
) -> tuple[WorkspaceFileLocator, ...]:
    role = payload.get("role")
    if role == "executor":
        target = "executor_prompt"
    elif role == "reviewer":
        target = "reviewer_prompt"
    else:
        return ()
    return tuple(
        locator
        for locator in plan.workspace_file_locators
        if locator.node_id == node.id and locator.target == target
    )


def _rendered_descriptor_matches_locator(
    payload: dict[str, object],
    descriptors_by_occurrence: dict[str, dict[str, object]],
    locator: WorkspaceFileLocator,
    plan: PreflightExecutionPlan,
    source: RunHistoryRecord | None,
) -> bool:
    descriptor = descriptors_by_occurrence.get(locator.occurrence_id)
    if descriptor is None:
        return False
    invocation_source = _mapping(payload.get("invocation_source"))
    return (
        descriptor.get("invocation_id") == _expected_invocation_id(payload)
        and descriptor.get("role") == payload.get("role")
        and _round_fields_match(payload, descriptor)
        and descriptor.get("source_kind") == invocation_source.get("source_kind")
        and descriptor.get("source_node_id") == invocation_source.get("source_node_id")
        and descriptor.get("source_commit") == invocation_source.get("source_commit")
        and descriptor.get("source_tree") == invocation_source.get("source_tree")
        and descriptor.get("candidate_sequence")
        == invocation_source.get("candidate_sequence")
        and descriptor.get("workspace_relative_path") == locator.workspace_relative_path
        and descriptor.get("target") == locator.target
        and descriptor.get("byte_source") == "git_blob"
        and descriptor.get("literal_path_verified") is True
        and descriptor.get("utf8_validated") is True
        and _rendered_descriptor_blob_matches(
            payload,
            descriptor,
            locator,
            plan,
            source,
        )
    )


def _descriptors_by_occurrence(
    descriptors: list[dict[str, object]],
) -> dict[str, dict[str, object]] | None:
    descriptors_by_occurrence: dict[str, dict[str, object]] = {}
    for descriptor in descriptors:
        occurrence_id = descriptor.get("occurrence_id")
        if not isinstance(occurrence_id, str):
            return None
        if occurrence_id in descriptors_by_occurrence:
            return None
        descriptors_by_occurrence[occurrence_id] = descriptor
    return descriptors_by_occurrence


def _expected_invocation_id(payload: dict[str, object]) -> str | None:
    node_id = payload.get("node_id")
    role = payload.get("role")
    task_id = payload.get("task_id")
    round_num = payload.get("round_num")
    audit_round_num = payload.get("audit_round_num")
    if (
        not isinstance(node_id, str)
        or not isinstance(role, str)
        or not isinstance(task_id, str)
        or isinstance(round_num, bool)
        or not isinstance(round_num, int)
    ):
        return None
    if audit_round_num is None:
        audit = ""
    elif isinstance(audit_round_num, bool) or not isinstance(audit_round_num, int):
        return None
    else:
        audit = f".audit-{audit_round_num}"
    return f"{node_id}.{role}.{task_id}{audit}.round-{round_num}"


def _round_fields_match(
    payload: dict[str, object],
    descriptor: dict[str, object],
) -> bool:
    round_num = int_field(payload, "round_num")
    descriptor_round_num = int_field(descriptor, "round_num")
    if round_num is None or descriptor_round_num is None:
        return False
    audit_round_num = nullable_int_field(payload, "audit_round_num")
    descriptor_audit_round_num = nullable_int_field(descriptor, "audit_round_num")
    if not audit_round_num.valid or not descriptor_audit_round_num.valid:
        return False
    return (
        descriptor_round_num == round_num
        and descriptor_audit_round_num.value == audit_round_num.value
    )


def _rendered_descriptor_blob_matches(
    payload: dict[str, object],
    descriptor: dict[str, object],
    locator: WorkspaceFileLocator,
    plan: PreflightExecutionPlan,
    source: RunHistoryRecord | None,
) -> bool:
    byte_size = descriptor.get("byte_size")
    if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 0:
        return False
    if not _is_hex_object(descriptor.get("git_blob")):
        return False
    if descriptor.get("git_file_mode") not in _SUPPORTED_RENDERED_FILE_MODES:
        return False
    injected = descriptor.get("injected_sha256")
    canonical = descriptor.get("canonical_blob_sha256")
    if not _is_sha256(injected) or not _is_sha256(canonical):
        return False
    if descriptor.get("source_kind") == "project":
        return _project_rendered_descriptor_matches(locator, descriptor)
    if canonical != injected:
        return False
    return _dynamic_rendered_descriptor_matches(
        payload, descriptor, locator, plan, source
    )


def _dynamic_rendered_descriptor_matches(
    payload: dict[str, object],
    descriptor: dict[str, object],
    locator: WorkspaceFileLocator,
    plan: PreflightExecutionPlan,
    source: RunHistoryRecord | None,
) -> bool:
    workspace_source = plan.workspace_source
    if source is None or workspace_source is None:
        return False
    git_blob = descriptor.get("git_blob")
    git_file_mode = descriptor.get("git_file_mode")
    byte_size = descriptor.get("byte_size")
    canonical = descriptor.get("canonical_blob_sha256")
    source_commit = descriptor.get("source_commit")
    source_tree = descriptor.get("source_tree")
    if (
        not isinstance(git_blob, str)
        or not isinstance(git_file_mode, str)
        or isinstance(byte_size, bool)
        or not isinstance(byte_size, int)
        or not isinstance(canonical, str)
        or not isinstance(source_commit, str)
        or not isinstance(source_tree, str)
    ):
        return False
    invocation_source = _mapping(payload.get("invocation_source"))
    bundle_path, bundle_ref = _source_bundle(source, invocation_source)
    if descriptor.get("source_kind") in {"node", "candidate"} and bundle_path is None:
        return False
    return workspace_blob_descriptor_matches(
        workspace_source.git_top_level,
        source_commit,
        source_tree,
        locator.git_top_relative_path,
        git_blob,
        git_file_mode,
        byte_size,
        canonical,
        workspace_source.object_format,
        bundle_path,
        bundle_ref,
    )


def _source_bundle(
    source: RunHistoryRecord,
    descriptor: dict[str, object],
) -> tuple[Path | None, str | None]:
    bundle_relative_path = descriptor.get("source_bundle_path")
    bundle_ref = descriptor.get("source_bundle_ref")
    if not isinstance(bundle_relative_path, str):
        return None, bundle_ref if isinstance(bundle_ref, str) else None
    bundle_path = contained_regular_file(source.run_dir, bundle_relative_path)
    if bundle_path is None:
        return None, bundle_ref if isinstance(bundle_ref, str) else None
    bundle_sha256 = descriptor.get("source_bundle_sha256")
    bundle_size_bytes = int_field(descriptor, "source_bundle_size_bytes")
    if not isinstance(bundle_sha256, str) or bundle_size_bytes is None:
        return None, bundle_ref if isinstance(bundle_ref, str) else None
    try:
        actual_size, actual_sha256 = file_size_and_sha256(bundle_path)
    except OSError:
        return None, bundle_ref if isinstance(bundle_ref, str) else None
    if actual_size != bundle_size_bytes or actual_sha256 != bundle_sha256:
        return None, bundle_ref if isinstance(bundle_ref, str) else None
    return bundle_path, bundle_ref if isinstance(bundle_ref, str) else None


def _project_rendered_descriptor_matches(
    locator: WorkspaceFileLocator,
    descriptor: dict[str, object],
) -> bool:
    if locator.git_blob is not None and descriptor.get("git_blob") != locator.git_blob:
        return False
    if (
        locator.git_file_mode is not None
        and descriptor.get("git_file_mode") != locator.git_file_mode
    ):
        return False
    if (
        locator.byte_size is not None
        and descriptor.get("byte_size") != locator.byte_size
    ):
        return False
    if locator.canonical_blob_sha256 is not None:
        return (
            descriptor.get("canonical_blob_sha256") == locator.canonical_blob_sha256
            and descriptor.get("injected_sha256") == locator.canonical_blob_sha256
        )
    return descriptor.get("canonical_blob_sha256") == descriptor.get("injected_sha256")


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _is_hex_object(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(char in "0123456789abcdef" for char in value)
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )

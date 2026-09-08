from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from crewplane.core.workspace.invocation_identity import invocation_slug
from crewplane.core.workspace.policy import safe_ref_component

from .fields import is_hex_object, is_nonempty_string, mapping_value


@dataclass(frozen=True)
class _PublicationExpectations:
    canonical_success: bool
    recorded_names: Mapping[str, object]
    scoped_names: Mapping[str, str]
    target_oids: Mapping[str, object]


@dataclass(frozen=True)
class _TemporaryRefContext:
    payload: Mapping[str, object]
    invocation_slug_value: str | None
    prefix: str
    coherent_oids: set[str]


def validate_ref_contracts(
    payload: Mapping[str, object],
    errors: list[str],
    hydrated_placement: bool,
) -> None:
    _validate_ref_publication(payload, errors, hydrated_placement)
    _validate_temporary_refs(payload, errors)


def _validate_ref_publication(
    payload: Mapping[str, object],
    errors: list[str],
    hydrated_placement: bool,
) -> None:
    workspace = mapping_value(payload.get("workspace"))
    lineage_producer = workspace.get("lineage_producer") is True
    if "ref_publication" not in payload:
        if _requires_ref_publication(payload, lineage_producer, hydrated_placement):
            errors.append("lineage result lacks ref publication phase")
        return
    if not lineage_producer and not is_discarded_lineage(payload):
        errors.append("non-lineage workspace has ref publication evidence")
        return
    publication = mapping_value(payload.get("ref_publication"))
    if publication.get("phase") not in {"prepared", "published", "removed"}:
        errors.append("lineage result lacks ref publication phase")
        return
    canonical_success = lineage_producer and payload.get("status") == "succeeded"
    _validate_publication_phase(publication, canonical_success, errors)
    _validate_publication_identity(payload, publication, errors)
    _validate_destinations(payload, publication, canonical_success, errors)


def _requires_ref_publication(
    payload: Mapping[str, object],
    lineage_producer: bool,
    hydrated_placement: bool,
) -> bool:
    return (
        lineage_producer
        and payload.get("status") == "succeeded"
        and not hydrated_placement
    )


def _validate_publication_phase(
    publication: Mapping[str, object],
    canonical_success: bool,
    errors: list[str],
) -> None:
    if canonical_success and publication.get("phase") == "prepared":
        errors.append("successful lineage has only prepared ref publication")


def _validate_publication_identity(
    payload: Mapping[str, object],
    publication: Mapping[str, object],
    errors: list[str],
) -> None:
    if publication.get("repository_id") != mapping_value(payload.get("git")).get(
        "repo_id"
    ):
        errors.append("ref publication repository mismatch")
    if not _publication_run_identity_matches(payload, publication):
        errors.append("ref publication run identity mismatch")
    for field in ("node_id", "task_id", "role", "round_num", "audit_round_num"):
        if publication.get(field) != payload.get(field):
            errors.append(f"ref publication {field} mismatch")


def is_discarded_lineage(payload: Mapping[str, object]) -> bool:
    result = mapping_value(payload.get("result"))
    return (
        payload.get("workspace_kind") == "worktree"
        and payload.get("role") == "executor"
        and result.get("lineage_produced") is False
        and result.get("lineage_discarded") is True
        and is_nonempty_string(result.get("lineage_discard_reason"))
        and "refs" not in payload
        and "bundle" not in payload
    )


def _validate_destinations(
    payload: Mapping[str, object],
    publication: Mapping[str, object],
    canonical_success: bool,
    errors: list[str],
) -> None:
    destinations = mapping_value(publication.get("destinations"))
    if set(destinations) != {"candidate", "result"}:
        errors.append("ref publication destinations are incomplete")
        return
    expectations = _publication_expectations(payload, publication, canonical_success)
    for label, value in destinations.items():
        _validate_destination(label, value, expectations, errors)


def _publication_expectations(
    payload: Mapping[str, object],
    publication: Mapping[str, object],
    canonical_success: bool,
) -> _PublicationExpectations:
    refs = mapping_value(payload.get("refs")) if canonical_success else {}
    result = mapping_value(payload.get("result")) if canonical_success else {}
    return _PublicationExpectations(
        canonical_success=canonical_success,
        recorded_names=refs,
        scoped_names=_expected_publication_names(payload, publication),
        target_oids={
            "candidate": result.get("candidate_commit"),
            "result": result.get("result_commit"),
        },
    )


def _validate_destination(
    label: str,
    value: object,
    expectations: _PublicationExpectations,
    errors: list[str],
) -> None:
    destination = mapping_value(value)
    if not _destination_has_valid_name_and_target(destination):
        errors.append("invalid ref publication destination")
        return
    _validate_destination_expected_oid(label, destination, errors)
    _validate_destination_names(label, destination, expectations, errors)
    _validate_destination_target(label, destination, expectations, errors)


def _destination_has_valid_name_and_target(
    destination: Mapping[str, object],
) -> bool:
    return is_nonempty_string(destination.get("name")) and is_hex_object(
        destination.get("target_oid")
    )


def _validate_destination_expected_oid(
    label: str,
    destination: Mapping[str, object],
    errors: list[str],
) -> None:
    expected_old_oid = destination.get("expected_old_oid")
    if "expected_old_oid" not in destination or (
        expected_old_oid is not None and not is_hex_object(expected_old_oid)
    ):
        errors.append(f"ref publication {label} expected OID is invalid")


def _validate_destination_names(
    label: str,
    destination: Mapping[str, object],
    expectations: _PublicationExpectations,
    errors: list[str],
) -> None:
    if expectations.canonical_success and destination.get(
        "name"
    ) != expectations.recorded_names.get(label):
        errors.append(f"ref publication {label} name mismatch")
    if expectations.scoped_names and destination.get(
        "name"
    ) != expectations.scoped_names.get(label):
        errors.append(f"ref publication {label} escapes invocation scope")


def _validate_destination_target(
    label: str,
    destination: Mapping[str, object],
    expectations: _PublicationExpectations,
    errors: list[str],
) -> None:
    if expectations.canonical_success and destination.get(
        "target_oid"
    ) != expectations.target_oids.get(label):
        errors.append(f"ref publication {label} target mismatch")


def _expected_publication_names(
    payload: Mapping[str, object], publication: Mapping[str, object]
) -> dict[str, str]:
    invocation_slug_value = _state_invocation_slug(payload)
    if invocation_slug_value is None:
        return {}
    base = (
        "refs/crewplane/runs/"
        f"{safe_ref_component(str(publication.get('run_key_name')))}/"
        f"{safe_ref_component(str(payload.get('node_id')))}/"
        f"{safe_ref_component(invocation_slug_value)}"
    )
    return {"candidate": f"{base}/candidate", "result": f"{base}/result"}


def _validate_temporary_refs(payload: Mapping[str, object], errors: list[str]) -> None:
    temporary_refs = payload.get("temporary_refs")
    if temporary_refs is None:
        return
    if not isinstance(temporary_refs, list):
        errors.append("temporary ref evidence is invalid")
        return
    invocation_slug_value = _state_invocation_slug(payload)
    prefix = (
        f"refs/crewplane/runs/{safe_ref_component(str(payload.get('run_key_name')))}/"
        f"imports/{safe_ref_component(str(payload.get('node_id')))}/"
        f"{safe_ref_component(str(invocation_slug_value))}/"
    )
    context = _TemporaryRefContext(
        payload=payload,
        invocation_slug_value=invocation_slug_value,
        prefix=prefix,
        coherent_oids=_coherent_ref_oids(payload),
    )
    for claim in temporary_refs:
        if not _valid_temporary_ref(mapping_value(claim), context):
            errors.append("temporary ref claim is contradictory")


def _valid_temporary_ref(
    record: Mapping[str, object],
    context: _TemporaryRefContext,
) -> bool:
    return (
        _temporary_ref_phase_is_valid(record)
        and _temporary_ref_owner_matches(record, context.payload)
        and _temporary_ref_repository_matches(record, context.payload)
        and _temporary_ref_is_invocation_scoped(record, context)
        and _temporary_ref_target_is_valid(record)
        and _temporary_ref_target_is_coherent(record, context.coherent_oids)
    )


def _temporary_ref_phase_is_valid(record: Mapping[str, object]) -> bool:
    return record.get("phase") in {"prepared", "removed"}


def _temporary_ref_owner_matches(
    record: Mapping[str, object],
    payload: Mapping[str, object],
) -> bool:
    return all(
        record.get(f"owner_{field}") == payload.get(field)
        for field in (
            "run_id",
            "node_id",
            "task_id",
            "role",
            "round_num",
            "audit_round_num",
        )
    )


def _temporary_ref_repository_matches(
    record: Mapping[str, object],
    payload: Mapping[str, object],
) -> bool:
    return record.get("repository_id") == mapping_value(payload.get("git")).get(
        "repo_id"
    )


def _temporary_ref_is_invocation_scoped(
    record: Mapping[str, object],
    context: _TemporaryRefContext,
) -> bool:
    name = record.get("name")
    return (
        isinstance(name, str)
        and context.invocation_slug_value is not None
        and name.startswith(context.prefix)
    )


def _temporary_ref_target_is_valid(record: Mapping[str, object]) -> bool:
    return is_hex_object(record.get("target_oid"))


def _temporary_ref_target_is_coherent(
    record: Mapping[str, object],
    coherent_oids: set[str],
) -> bool:
    return record.get("target_oid") in coherent_oids


def _coherent_ref_oids(payload: Mapping[str, object]) -> set[str]:
    oids = _source_commit_oids(mapping_value(payload.get("source")))
    result = mapping_value(payload.get("result"))
    oids.update(
        value
        for value in (result.get("candidate_commit"), result.get("result_commit"))
        if isinstance(value, str)
    )
    return oids


def _state_invocation_slug(payload: Mapping[str, object]) -> str | None:
    node_id = payload.get("node_id")
    task_id = payload.get("task_id")
    round_num = payload.get("round_num")
    audit_round_num = payload.get("audit_round_num")
    if not (
        isinstance(node_id, str)
        and node_id
        and isinstance(task_id, str)
        and task_id
        and isinstance(round_num, int)
        and not isinstance(round_num, bool)
        and (
            audit_round_num is None
            or isinstance(audit_round_num, int)
            and not isinstance(audit_round_num, bool)
        )
    ):
        return None
    return invocation_slug(node_id, task_id, audit_round_num, round_num)


def _source_commit_oids(source: Mapping[str, object]) -> set[str]:
    commits = {value for value in (source.get("commit"),) if isinstance(value, str)}
    upstreams = source.get("upstream_sources")
    if isinstance(upstreams, list):
        for upstream in upstreams:
            commits.update(_source_commit_oids(mapping_value(upstream)))
    return commits


def _publication_run_identity_matches(
    payload: Mapping[str, object], publication: Mapping[str, object]
) -> bool:
    expected = (publication.get("run_id"), publication.get("run_key_name"))
    if expected == (payload.get("run_id"), payload.get("run_key_name")):
        return True
    origin = mapping_value(payload.get("resume_origin"))
    while origin:
        if expected == (origin.get("source_run_id"), origin.get("source_run_key_name")):
            return True
        origin = mapping_value(origin.get("source_resume_origin"))
    return False

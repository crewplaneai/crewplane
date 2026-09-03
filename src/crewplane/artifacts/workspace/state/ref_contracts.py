from __future__ import annotations

from collections.abc import Mapping

from crewplane.core.workspace.invocation_identity import invocation_slug
from crewplane.core.workspace.policy import safe_ref_component


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
    workspace = _mapping(payload.get("workspace"))
    lineage_producer = workspace.get("lineage_producer") is True
    if "ref_publication" not in payload:
        if (
            lineage_producer
            and payload.get("status") == "succeeded"
            and not hydrated_placement
        ):
            errors.append("lineage result lacks ref publication phase")
        return
    if not lineage_producer and not _is_discarded_lineage(payload):
        errors.append("non-lineage workspace has ref publication evidence")
        return
    publication = _mapping(payload.get("ref_publication"))
    if publication.get("phase") not in {"prepared", "published", "removed"}:
        errors.append("lineage result lacks ref publication phase")
        return
    canonical_success = lineage_producer and payload.get("status") == "succeeded"
    if canonical_success and publication.get("phase") == "prepared":
        errors.append("successful lineage has only prepared ref publication")
    if publication.get("repository_id") != _mapping(payload.get("git")).get("repo_id"):
        errors.append("ref publication repository mismatch")
    if not _publication_run_identity_matches(payload, publication):
        errors.append("ref publication run identity mismatch")
    for field in ("node_id", "task_id", "role", "round_num", "audit_round_num"):
        if publication.get(field) != payload.get(field):
            errors.append(f"ref publication {field} mismatch")
    _validate_destinations(payload, publication, errors, canonical_success)


def _is_discarded_lineage(payload: Mapping[str, object]) -> bool:
    result = _mapping(payload.get("result"))
    return (
        payload.get("workspace_kind") == "worktree"
        and payload.get("role") == "executor"
        and result.get("lineage_produced") is False
        and result.get("lineage_discarded") is True
        and _nonempty_string(result.get("lineage_discard_reason"))
        and "refs" not in payload
        and "bundle" not in payload
    )


def _validate_destinations(
    payload: Mapping[str, object],
    publication: Mapping[str, object],
    errors: list[str],
    canonical_success: bool,
) -> None:
    destinations = _mapping(publication.get("destinations"))
    if set(destinations) != {"candidate", "result"}:
        errors.append("ref publication destinations are incomplete")
        return
    refs = _mapping(payload.get("refs")) if canonical_success else {}
    result = _mapping(payload.get("result")) if canonical_success else {}
    expected_targets = {
        "candidate": result.get("candidate_commit"),
        "result": result.get("result_commit"),
    }
    expected_names = _expected_publication_names(payload, publication)
    for label, value in destinations.items():
        destination = _mapping(value)
        if not _nonempty_string(destination.get("name")) or not _object_id(
            destination.get("target_oid")
        ):
            errors.append("invalid ref publication destination")
            continue
        if "expected_old_oid" not in destination or (
            destination.get("expected_old_oid") is not None
            and not _object_id(destination.get("expected_old_oid"))
        ):
            errors.append(f"ref publication {label} expected OID is invalid")
        if canonical_success and destination.get("name") != refs.get(label):
            errors.append(f"ref publication {label} name mismatch")
        if expected_names and destination.get("name") != expected_names[label]:
            errors.append(f"ref publication {label} escapes invocation scope")
        if canonical_success and destination.get("target_oid") != expected_targets.get(
            label
        ):
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
    coherent_oids = _coherent_ref_oids(payload)
    for claim in temporary_refs:
        if not _valid_temporary_ref(
            payload, _mapping(claim), invocation_slug_value, prefix, coherent_oids
        ):
            errors.append("temporary ref claim is contradictory")


def _valid_temporary_ref(
    payload: Mapping[str, object],
    record: Mapping[str, object],
    invocation_slug_value: str | None,
    prefix: str,
    coherent_oids: set[str],
) -> bool:
    return (
        record.get("phase") in {"prepared", "removed"}
        and record.get("owner_run_id") == payload.get("run_id")
        and record.get("owner_node_id") == payload.get("node_id")
        and record.get("owner_task_id") == payload.get("task_id")
        and record.get("owner_role") == payload.get("role")
        and record.get("owner_round_num") == payload.get("round_num")
        and record.get("owner_audit_round_num") == payload.get("audit_round_num")
        and record.get("repository_id") == _mapping(payload.get("git")).get("repo_id")
        and isinstance(record.get("name"), str)
        and invocation_slug_value is not None
        and str(record.get("name")).startswith(prefix)
        and _object_id(record.get("target_oid"))
        and record.get("target_oid") in coherent_oids
    )


def _coherent_ref_oids(payload: Mapping[str, object]) -> set[str]:
    oids = _source_commit_oids(_mapping(payload.get("source")))
    result = _mapping(payload.get("result"))
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
            commits.update(_source_commit_oids(_mapping(upstream)))
    return commits


def _publication_run_identity_matches(
    payload: Mapping[str, object], publication: Mapping[str, object]
) -> bool:
    expected = (publication.get("run_id"), publication.get("run_key_name"))
    if expected == (payload.get("run_id"), payload.get("run_key_name")):
        return True
    origin = _mapping(payload.get("resume_origin"))
    while origin:
        if expected == (origin.get("source_run_id"), origin.get("source_run_key_name")):
            return True
        origin = _mapping(origin.get("source_resume_origin"))
    return False


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _object_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )

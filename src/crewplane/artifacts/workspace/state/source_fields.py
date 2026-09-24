from __future__ import annotations

from collections.abc import Mapping

# Source key, invocation key, and the persisted diagnostic label, in error order.
SOURCE_FIELDS = (
    ("kind", "source_kind", "source_kind"),
    ("node_id", "source_node_id", "source_node_id"),
    ("commit", "source_commit", "source_commit"),
    ("tree", "source_tree", "source_tree"),
    ("candidate_sequence", "candidate_sequence", "candidate_sequence"),
    ("bundle_path", "source_bundle_path", "bundle_path"),
    ("bundle_sha256", "source_bundle_sha256", "bundle_sha256"),
    ("bundle_size_bytes", "source_bundle_size_bytes", "bundle_size_bytes"),
    ("bundle_ref", "source_bundle_ref", "bundle_ref"),
)


def invocation_source_payload(source: Mapping[str, object]) -> dict[str, object]:
    return {
        invocation_field: source[source_field]
        for source_field, invocation_field, _ in SOURCE_FIELDS
        if source_field in source
    }


def source_field_mismatches(
    source: Mapping[str, object], invocation: Mapping[str, object]
) -> tuple[str, ...]:
    return tuple(
        label
        for source_field, invocation_field, label in SOURCE_FIELDS
        if source.get(source_field) != invocation.get(invocation_field)
    )


def normalized_source_descriptor(source: Mapping[str, object]) -> dict[str, object]:
    return {field: source.get(field) for field, _, _ in SOURCE_FIELDS}


def source_matches_result_fields(
    source: Mapping[str, object], result: Mapping[str, object]
) -> bool:
    return (
        source.get("commit") == result.get("result_commit")
        and source.get("tree") == result.get("result_tree")
        and source.get("candidate_sequence") == 1
    )


def source_matches_bundle_fields(
    source: Mapping[str, object], bundle: Mapping[str, object]
) -> bool:
    return (
        source.get("bundle_path") == bundle.get("path")
        and isinstance(source.get("bundle_sha256"), str)
        and source.get("bundle_sha256") == bundle.get("sha256")
        and source.get("bundle_size_bytes") == bundle.get("size_bytes")
    )

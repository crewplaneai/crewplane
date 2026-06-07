from __future__ import annotations

from dataclasses import replace

from .compile_state import CompileState, PreflightCompileOptions, extend_diagnostics
from .secrets import (
    FINGERPRINT_SCHEMA_VERSION,
    FingerprintKeyProvider,
    fingerprint_payload,
)


def load_fingerprint_key_if_needed(
    options: PreflightCompileOptions,
    state: CompileState,
) -> None:
    if not state.sensitive_values_required:
        return
    if state.fingerprint_key is not None:
        return
    result = FingerprintKeyProvider(options.orchestrator_dir).load_key(
        options.fingerprint_key_policy
    )
    extend_diagnostics(state, result.diagnostics)
    if result.diagnostics:
        return
    state.fingerprint_key = result.key
    state.fingerprint_key_persisted = result.persisted


def backfill_value_fingerprints(state: CompileState) -> None:
    key = state.fingerprint_key
    if key is None:
        return
    for record in state.value_fingerprints:
        raw_value = record.pop("value")
        payload = {
            "fingerprint_schema_version": FINGERPRINT_SCHEMA_VERSION,
            "key": record["key"],
            "kind": record["kind"],
            "sensitive": record["sensitive"],
            "value": raw_value,
        }
        record["fingerprint_schema_version"] = FINGERPRINT_SCHEMA_VERSION
        record["fingerprint"] = fingerprint_payload(key, payload)
    backfill_static_value_reference_fingerprints(state)


def backfill_static_value_reference_fingerprints(state: CompileState) -> None:
    fingerprints_by_value = {
        (record["kind"], record["key"], record["sensitive"]): record["fingerprint"]
        for record in state.value_fingerprints
        if "fingerprint" in record
    }
    for occurrence_id, reference in tuple(state.static_value_references.items()):
        if not reference.sensitive:
            continue
        fingerprint = fingerprints_by_value.get((reference.kind, reference.key, "true"))
        if fingerprint is None:
            continue
        state.static_value_references[occurrence_id] = replace(
            reference,
            fingerprint=fingerprint,
        )


def persisted_value_fingerprints(state: CompileState) -> list[dict[str, str]]:
    return [
        {
            "fingerprint": record["fingerprint"],
            "fingerprint_schema_version": record["fingerprint_schema_version"],
            "key": record["key"],
            "kind": record["kind"],
            "sensitive": record["sensitive"],
        }
        for record in state.value_fingerprints
        if "fingerprint" in record
    ]

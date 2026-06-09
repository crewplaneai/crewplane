from __future__ import annotations

from orchestrator_cli.architecture.contracts import JsonObject
from orchestrator_cli.version import SCHEMA_VERSION

from .secrets import FINGERPRINT_PAYLOAD_VERSION

CURRENT_FINGERPRINT_METADATA_FIELD = "payload_version"
CURRENT_RUNTIME_CONFIG_SCHEMA_FIELD = "schema_version"
CURRENT_VALUE_FINGERPRINT_VERSION_FIELD = "fingerprint_payload_version"
LEGACY_FINGERPRINT_METADATA_FIELDS = {"schema_version"}
LEGACY_RUNTIME_CONFIG_SCHEMA_FIELDS = {
    "config_schema_version",
    "workflow_schema_version",
}
LEGACY_VALUE_FINGERPRINT_VERSION_FIELDS = {"fingerprint_schema_version"}


def validate_supported_plan_schema_version(value: str) -> str:
    if value != SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported preflight plan schema version '{value}'. "
            f"Expected '{SCHEMA_VERSION}'."
        )
    return value


def validate_current_execution_plan_shape(
    runtime_config_snapshot: JsonObject,
    fingerprint_metadata: JsonObject,
    value_fingerprints: list[dict[str, str]],
) -> None:
    _validate_current_runtime_snapshot_shape(runtime_config_snapshot)
    _validate_current_fingerprint_metadata_shape(fingerprint_metadata)
    _validate_current_value_fingerprint_shape(value_fingerprints)


def _validate_current_runtime_snapshot_shape(snapshot: JsonObject) -> None:
    _reject_legacy_plan_fields(
        snapshot,
        LEGACY_RUNTIME_CONFIG_SCHEMA_FIELDS,
        "runtime config snapshot",
    )
    if CURRENT_RUNTIME_CONFIG_SCHEMA_FIELD not in snapshot:
        raise ValueError(
            "Preflight plan runtime config snapshot must include "
            f"'{CURRENT_RUNTIME_CONFIG_SCHEMA_FIELD}'."
        )
    if snapshot[CURRENT_RUNTIME_CONFIG_SCHEMA_FIELD] != SCHEMA_VERSION:
        raise ValueError(
            "Preflight plan runtime config snapshot schema_version must be "
            f"'{SCHEMA_VERSION}'."
        )
    for integration_name in ("invoker", "artifacts", "ui"):
        integration_payload = snapshot.get(integration_name)
        if isinstance(integration_payload, dict):
            _reject_legacy_plan_fields(
                integration_payload,
                {"api_version"},
                f"runtime config '{integration_name}' integration",
            )


def _validate_current_fingerprint_metadata_shape(metadata: JsonObject) -> None:
    _reject_legacy_plan_fields(
        metadata,
        LEGACY_FINGERPRINT_METADATA_FIELDS,
        "fingerprint metadata",
    )
    if CURRENT_FINGERPRINT_METADATA_FIELD not in metadata:
        raise ValueError(
            "Preflight plan fingerprint metadata must include "
            f"'{CURRENT_FINGERPRINT_METADATA_FIELD}'."
        )
    if metadata[CURRENT_FINGERPRINT_METADATA_FIELD] != FINGERPRINT_PAYLOAD_VERSION:
        raise ValueError(
            "Preflight plan fingerprint metadata payload_version must be "
            f"'{FINGERPRINT_PAYLOAD_VERSION}'."
        )


def _validate_current_value_fingerprint_shape(
    records: list[dict[str, str]],
) -> None:
    for index, record in enumerate(records):
        _reject_legacy_plan_fields(
            record,
            LEGACY_VALUE_FINGERPRINT_VERSION_FIELDS,
            f"value fingerprint at index {index}",
        )
        if CURRENT_VALUE_FINGERPRINT_VERSION_FIELD not in record:
            raise ValueError(
                "Preflight plan value fingerprint at index "
                f"{index} must include '{CURRENT_VALUE_FINGERPRINT_VERSION_FIELD}'."
            )
        if (
            record[CURRENT_VALUE_FINGERPRINT_VERSION_FIELD]
            != FINGERPRINT_PAYLOAD_VERSION
        ):
            raise ValueError(
                "Preflight plan value fingerprint at index "
                f"{index} payload version must be '{FINGERPRINT_PAYLOAD_VERSION}'."
            )


def _reject_legacy_plan_fields(
    payload: dict[str, object],
    legacy_fields: set[str],
    payload_label: str,
) -> None:
    present_fields = sorted(field for field in legacy_fields if field in payload)
    if not present_fields:
        return
    joined_fields = ", ".join(present_fields)
    raise ValueError(
        f"Unsupported legacy preflight plan {payload_label} field(s): {joined_fields}."
    )

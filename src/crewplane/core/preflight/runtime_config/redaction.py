"""Recursive redaction for arbitrary JSON-compatible config snapshots.

The public preflight models use typed fields or JsonObject. This module keeps
the traversal boundary JSON-compatible while walking nested user config values
for deterministic signatures and persisted plans.
"""

from __future__ import annotations

import re
from typing import Literal

from crewplane.architecture.contracts import (
    CanonicalIntegrationConfig,
    JsonObject,
    JsonValue,
    json_pointer,
    redacted_integration_option_value,
    sensitive_integration_option_pointers,
    transform_sensitive_integration_options,
)

from ..secrets import FINGERPRINT_PAYLOAD_VERSION, fingerprint_payload

_SENSITIVE_CONFIG_PATH_PATTERN = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|credential|private)",
    re.IGNORECASE,
)
_SENSITIVE_ARGV_PATTERN = re.compile(
    r"^(?:--|(?=[A-Za-z_][A-Za-z0-9_]*=))"
    r"[^=\s]*(?:secret|token|password|passwd|api[_-]?key|credential|private)"
    r"[^=\s]*(?:=.*)?$",
    re.IGNORECASE,
)
_ARGV_FIELD_NAMES = frozenset({"argv", "cli_cmd", "extra_args"})
_ARGV_SCALAR_FIELD_NAMES = frozenset({"model_arg", "prompt_transport_arg"})
type RedactionOutput = Literal["redacted", "fingerprinted"]


def config_value_handle(path: str) -> str:
    return f"config:{path}"


def redact_sensitive_config(
    payload: JsonObject,
    root_path: tuple[str, ...] = ("agents",),
) -> tuple[JsonObject, list[str]]:
    redacted, paths, _ = _redact_sensitive_value(
        payload,
        root_path,
        None,
        "redacted",
    )
    return _ensure_dict(redacted), sorted(paths)


def redact_sensitive_config_with_fingerprints(
    payload: JsonObject,
    fingerprint_key: bytes | None,
    root_path: tuple[str, ...] = ("agents",),
) -> tuple[JsonObject, list[dict[str, str]]]:
    redacted, _, fingerprints = _redact_sensitive_value(
        payload,
        root_path,
        fingerprint_key,
        "fingerprinted",
    )
    return _ensure_dict(redacted), sorted(fingerprints, key=lambda item: item["path"])


def sensitive_integration_option_paths(
    integration_name: str,
    config: CanonicalIntegrationConfig,
) -> list[str]:
    path_prefix = json_pointer(("integrations", integration_name, "options"))
    return sorted(
        f"{path_prefix}{pointer}"
        for pointer in sensitive_integration_option_pointers(config)
    )


def integration_with_sensitive_option_fingerprints(
    config: CanonicalIntegrationConfig,
    integration_name: str,
    fingerprint_key: bytes | None,
) -> tuple[CanonicalIntegrationConfig, list[dict[str, str]]]:
    fingerprints: list[dict[str, str]] = []

    def redact_sensitive_value(pointer: str, value: JsonValue) -> JsonValue:
        path_prefix = json_pointer(("integrations", integration_name, "options"))
        path = f"{path_prefix}{pointer}"
        fingerprint = config_fingerprint(fingerprint_key, path, value)
        redacted = redacted_option_value(
            fingerprint=fingerprint,
            value_handle=config_value_handle(path),
        )
        if fingerprint is not None:
            fingerprints.append({"path": path, "fingerprint": fingerprint})
        return redacted

    redacted_options, sensitive_pointers = transform_sensitive_integration_options(
        config.options,
        sensitive_integration_option_pointers(config),
        redact_sensitive_value,
    )
    if not sensitive_pointers:
        return config.model_copy(update={"option_fingerprints": []}), []

    fingerprints.sort(key=lambda item: item["path"])

    return (
        config.with_generated_redaction(
            options=redacted_options,
            sensitive_options=sorted(sensitive_pointers),
            option_fingerprints=fingerprints,
        ),
        fingerprints,
    )


def redacted_option_value(
    fingerprint: str | None = None,
    value_handle: str | None = None,
) -> JsonObject:
    return redacted_integration_option_value(
        fingerprint=fingerprint,
        value_handle=value_handle,
    )


def config_fingerprint(
    fingerprint_key: bytes | None,
    path: str,
    value: JsonValue,
) -> str | None:
    if fingerprint_key is None:
        return None
    return fingerprint_payload(
        fingerprint_key,
        {
            "fingerprint_payload_version": FINGERPRINT_PAYLOAD_VERSION,
            "kind": "config",
            "path": path,
            "sensitive": "true",
            "value": value,
        },
    )


def _redact_sensitive_value(
    value: JsonValue,
    path: tuple[str, ...],
    fingerprint_key: bytes | None,
    output: RedactionOutput,
    list_parent: str | None = None,
) -> tuple[JsonValue, list[str], list[dict[str, str]]]:
    if _is_sensitive_config_value(value, path, list_parent):
        return _redacted_sensitive_leaf(value, path, fingerprint_key, output)
    if isinstance(value, dict):
        return _redacted_sensitive_dict(value, path, fingerprint_key, output)
    if isinstance(value, list):
        return _redacted_sensitive_list(value, path, fingerprint_key, output)
    return value, [], []


def _redacted_sensitive_leaf(
    value: JsonValue,
    path: tuple[str, ...],
    fingerprint_key: bytes | None,
    output: RedactionOutput,
) -> tuple[JsonObject, list[str], list[dict[str, str]]]:
    path_label = _path_label(path)
    fingerprint = config_fingerprint(fingerprint_key, path_label, value)
    redacted_value: JsonObject = {"redacted": True}
    if output == "fingerprinted":
        redacted_value["value_handle"] = config_value_handle(path_label)
    if fingerprint is None:
        return redacted_value, [path_label], []
    redacted_value["fingerprint"] = fingerprint
    return (
        redacted_value,
        [path_label],
        [{"path": path_label, "fingerprint": fingerprint}],
    )


def _redacted_sensitive_dict(
    value: dict[str, JsonValue],
    path: tuple[str, ...],
    fingerprint_key: bytes | None,
    output: RedactionOutput,
) -> tuple[JsonObject, list[str], list[dict[str, str]]]:
    paths: list[str] = []
    fingerprints: list[dict[str, str]] = []
    redacted: JsonObject = {}
    for key, child in sorted(value.items()):
        child_value, child_paths, child_fingerprints = _redact_sensitive_value(
            child,
            (*path, str(key)),
            fingerprint_key,
            output,
        )
        redacted[str(key)] = child_value
        paths.extend(child_paths)
        fingerprints.extend(child_fingerprints)
    boundary_secret = _command_field_boundary_secret(value, path)
    if boundary_secret is not None:
        secret_value, secret_path = boundary_secret
        path_label = _path_label(secret_path)
        if path_label not in paths:
            redacted_value, child_paths, child_fingerprints = _redacted_sensitive_leaf(
                secret_value,
                secret_path,
                fingerprint_key,
                output,
            )
            _replace_first_extra_arg(redacted, redacted_value)
            paths.extend(child_paths)
            fingerprints.extend(child_fingerprints)
    return redacted, paths, fingerprints


def _redacted_sensitive_list(
    value: list[JsonValue],
    path: tuple[str, ...],
    fingerprint_key: bytes | None,
    output: RedactionOutput,
) -> tuple[list[JsonValue], list[str], list[dict[str, str]]]:
    paths: list[str] = []
    fingerprints: list[dict[str, str]] = []
    redacted_list: list[JsonValue] = []
    sensitive_indices = _sensitive_argv_indices(value, path)
    for index, child in enumerate(value):
        child_path = (*path, str(index))
        if index in sensitive_indices:
            sensitive_value, child_paths, child_fingerprints = _redacted_sensitive_leaf(
                child,
                child_path,
                fingerprint_key,
                output,
            )
            redacted_list.append(sensitive_value)
            paths.extend(child_paths)
            fingerprints.extend(child_fingerprints)
            continue
        child_value, child_paths, child_fingerprints = _redact_sensitive_value(
            child,
            child_path,
            fingerprint_key,
            output,
            path[-1] if path else None,
        )
        redacted_list.append(child_value)
        paths.extend(child_paths)
        fingerprints.extend(child_fingerprints)
    return redacted_list, paths, fingerprints


def _is_sensitive_config_value(
    value: JsonValue,
    path: tuple[str, ...],
    list_parent: str | None,
) -> bool:
    if not path:
        return False
    if any(_SENSITIVE_CONFIG_PATH_PATTERN.search(segment) for segment in path):
        return not isinstance(value, (dict, list))
    if path[-1] in _ARGV_SCALAR_FIELD_NAMES and isinstance(value, str):
        return "=" in value and _SENSITIVE_ARGV_PATTERN.search(value) is not None
    if list_parent in _ARGV_FIELD_NAMES and isinstance(value, str):
        return "=" in value and _SENSITIVE_ARGV_PATTERN.search(value) is not None
    return False


def _command_field_boundary_secret(
    value: dict[str, JsonValue],
    path: tuple[str, ...],
) -> tuple[str, tuple[str, ...]] | None:
    cli_cmd = value.get("cli_cmd")
    extra_args = value.get("extra_args")
    if not isinstance(cli_cmd, list) or not cli_cmd:
        return None
    if not isinstance(extra_args, list) or not extra_args:
        return None
    split_flag = cli_cmd[-1]
    secret_value = extra_args[0]
    if not isinstance(split_flag, str) or not isinstance(secret_value, str):
        return None
    if "=" in split_flag or _SENSITIVE_ARGV_PATTERN.search(split_flag) is None:
        return None
    return secret_value, (*path, "extra_args", "0")


def _replace_first_extra_arg(
    redacted: JsonObject,
    value: JsonObject,
) -> None:
    extra_args = redacted.get("extra_args")
    if not isinstance(extra_args, list) or not extra_args:
        raise TypeError("Redacted agent extra_args must remain a non-empty list.")
    extra_args[0] = value


def _sensitive_argv_indices(
    value: list[JsonValue],
    path: tuple[str, ...],
) -> set[int]:
    if not _is_argv_token_list(path):
        return set()

    sensitive_indices: set[int] = set()
    previous_was_sensitive_split_flag = False
    for index, child in enumerate(value):
        if previous_was_sensitive_split_flag:
            sensitive_indices.add(index)
        previous_was_sensitive_split_flag = False
        if not isinstance(child, str):
            continue
        if _SENSITIVE_ARGV_PATTERN.search(child) is None:
            continue
        if "=" in child:
            sensitive_indices.add(index)
            continue
        previous_was_sensitive_split_flag = True
    return sensitive_indices


def _is_argv_token_list(path: tuple[str, ...]) -> bool:
    if not path:
        return False
    if path[-1] in _ARGV_FIELD_NAMES:
        return True
    return len(path) >= 2 and path[-2] == "run" and "setup_profiles" in path


def _path_label(path: tuple[str, ...]) -> str:
    return ".".join(path)


def _ensure_dict(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        raise TypeError("Runtime config payload must remain a dictionary.")
    return dict(value)

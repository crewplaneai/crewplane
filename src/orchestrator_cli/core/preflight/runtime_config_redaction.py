from __future__ import annotations

import re
from typing import Any

from .secrets import FINGERPRINT_SCHEMA_VERSION, fingerprint_payload

_SENSITIVE_CONFIG_PATH_PATTERN = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|credential|private)",
    re.IGNORECASE,
)
_SENSITIVE_EXTRA_ARG_PATTERN = re.compile(
    r"^--[^=\s]*(?:secret|token|password|passwd|api[_-]?key|credential|private)[^=\s]*(?:=.*)?$",
    re.IGNORECASE,
)


def config_value_handle(path: str) -> str:
    return f"config:{path}"


def redact_sensitive_config(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    redacted, paths = _redact_sensitive_value(payload, ("agents",), None)
    return _ensure_dict(redacted), sorted(paths)


def redact_sensitive_config_with_fingerprints(
    payload: dict[str, Any],
    fingerprint_key: bytes | None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    redacted, _, fingerprints = _redact_sensitive_value_with_fingerprints(
        payload,
        ("agents",),
        fingerprint_key,
    )
    return _ensure_dict(redacted), sorted(fingerprints, key=lambda item: item["path"])


def sensitive_integration_option_keys(config: Any) -> set[str]:
    return {
        key
        for key in config.options
        if key in config.sensitive_options
        or _SENSITIVE_CONFIG_PATH_PATTERN.search(key) is not None
    }


def sensitive_integration_option_paths(
    integration_name: str,
    config: Any,
) -> list[str]:
    return sorted(
        f"integrations.{integration_name}.options.{key}"
        for key in sensitive_integration_option_keys(config)
    )


def integration_with_sensitive_option_fingerprints(
    config: Any,
    integration_name: str,
    fingerprint_key: bytes | None,
) -> tuple[Any, list[dict[str, str]]]:
    sensitive_keys = sensitive_integration_option_keys(config)
    if not sensitive_keys:
        return config, []

    redacted_options: dict[str, Any] = {}
    fingerprints: list[dict[str, str]] = []
    for key, value in sorted(config.options.items()):
        if key not in sensitive_keys:
            redacted_options[key] = value
            continue
        path = f"integrations.{integration_name}.options.{key}"
        fingerprint = config_fingerprint(fingerprint_key, path, value)
        redacted_options[key] = redacted_option_value(
            value,
            fingerprint,
            config_value_handle(path),
        )
        if fingerprint is not None:
            fingerprints.append({"path": path, "fingerprint": fingerprint})

    return (
        config.model_copy(
            update={
                "options": redacted_options,
                "option_fingerprints": fingerprints,
                "sensitive_options": sorted(sensitive_keys),
            }
        ),
        fingerprints,
    )


def redacted_option_value(
    value: Any,
    fingerprint: str | None = None,
    value_handle: str | None = None,
) -> dict[str, Any]:
    redacted = dict(value) if _is_redacted_option_payload(value) else {"redacted": True}
    if fingerprint is not None:
        redacted["fingerprint"] = fingerprint
    if value_handle is not None:
        redacted["value_handle"] = value_handle
    return redacted


def config_fingerprint(
    fingerprint_key: bytes | None,
    path: str,
    value: Any,
) -> str | None:
    if fingerprint_key is None:
        return None
    return fingerprint_payload(
        fingerprint_key,
        {
            "fingerprint_schema_version": FINGERPRINT_SCHEMA_VERSION,
            "kind": "config",
            "path": path,
            "sensitive": "true",
            "value": value,
        },
    )


def _is_redacted_option_payload(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("redacted") is not True:
        return False
    return set(value) <= {"fingerprint", "redacted", "value_handle"}


def _redact_sensitive_value(
    value: Any,
    path: tuple[str, ...],
    list_parent: str | None,
) -> tuple[Any, list[str]]:
    if _is_sensitive_config_value(value, path, list_parent):
        return {"redacted": True}, [_path_label(path)]
    if isinstance(value, dict):
        paths: list[str] = []
        redacted: dict[str, Any] = {}
        for key, child in sorted(value.items()):
            child_value, child_paths = _redact_sensitive_value(
                child,
                (*path, str(key)),
                None,
            )
            redacted[str(key)] = child_value
            paths.extend(child_paths)
        return redacted, paths
    if isinstance(value, list):
        return _redact_sensitive_list(value, path)
    return value, []


def _redact_sensitive_value_with_fingerprints(
    value: Any,
    path: tuple[str, ...],
    fingerprint_key: bytes | None,
    list_parent: str | None = None,
) -> tuple[Any, list[str], list[dict[str, str]]]:
    if _is_sensitive_config_value(value, path, list_parent):
        return _redacted_sensitive_leaf(value, path, fingerprint_key)
    if isinstance(value, dict):
        return _redacted_sensitive_dict(value, path, fingerprint_key)
    if isinstance(value, list):
        return _redacted_sensitive_list(value, path, fingerprint_key)
    return value, [], []


def _redacted_sensitive_leaf(
    value: Any,
    path: tuple[str, ...],
    fingerprint_key: bytes | None,
) -> tuple[dict[str, Any], list[str], list[dict[str, str]]]:
    path_label = _path_label(path)
    fingerprint = config_fingerprint(fingerprint_key, path_label, value)
    redacted_value: dict[str, Any] = {
        "redacted": True,
        "value_handle": config_value_handle(path_label),
    }
    if fingerprint is None:
        return redacted_value, [path_label], []
    redacted_value["fingerprint"] = fingerprint
    return (
        redacted_value,
        [path_label],
        [{"path": path_label, "fingerprint": fingerprint}],
    )


def _redacted_sensitive_dict(
    value: dict[Any, Any],
    path: tuple[str, ...],
    fingerprint_key: bytes | None,
) -> tuple[dict[str, Any], list[str], list[dict[str, str]]]:
    paths: list[str] = []
    fingerprints: list[dict[str, str]] = []
    redacted: dict[str, Any] = {}
    for key, child in sorted(value.items()):
        child_value, child_paths, child_fingerprints = (
            _redact_sensitive_value_with_fingerprints(
                child,
                (*path, str(key)),
                fingerprint_key,
            )
        )
        redacted[str(key)] = child_value
        paths.extend(child_paths)
        fingerprints.extend(child_fingerprints)
    return redacted, paths, fingerprints


def _redacted_sensitive_list(
    value: list[Any],
    path: tuple[str, ...],
    fingerprint_key: bytes | None,
) -> tuple[list[Any], list[str], list[dict[str, str]]]:
    paths = []
    fingerprints = []
    redacted_list = []
    sensitive_indices = _sensitive_extra_arg_indices(value, path)
    for index, child in enumerate(value):
        child_path = (*path, str(index))
        if index in sensitive_indices:
            child_value, child_paths, child_fingerprints = _redacted_sensitive_leaf(
                child,
                child_path,
                fingerprint_key,
            )
            redacted_list.append(child_value)
            paths.extend(child_paths)
            fingerprints.extend(child_fingerprints)
            continue
        child_value, child_paths, child_fingerprints = (
            _redact_sensitive_value_with_fingerprints(
                child,
                child_path,
                fingerprint_key,
                path[-1] if path else None,
            )
        )
        redacted_list.append(child_value)
        paths.extend(child_paths)
        fingerprints.extend(child_fingerprints)
    return redacted_list, paths, fingerprints


def _is_sensitive_config_value(
    value: Any,
    path: tuple[str, ...],
    list_parent: str | None,
) -> bool:
    if not path:
        return False
    if any(_SENSITIVE_CONFIG_PATH_PATTERN.search(segment) for segment in path):
        return not isinstance(value, (dict, list))
    if list_parent == "extra_args" and isinstance(value, str):
        return "=" in value and _SENSITIVE_EXTRA_ARG_PATTERN.search(value) is not None
    return False


def _redact_sensitive_list(
    value: list[Any],
    path: tuple[str, ...],
) -> tuple[list[Any], list[str]]:
    paths = []
    redacted_list = []
    sensitive_indices = _sensitive_extra_arg_indices(value, path)
    for index, child in enumerate(value):
        child_path = (*path, str(index))
        if index in sensitive_indices:
            redacted_list.append({"redacted": True})
            paths.append(_path_label(child_path))
            continue
        child_value, child_paths = _redact_sensitive_value(
            child,
            child_path,
            path[-1] if path else None,
        )
        redacted_list.append(child_value)
        paths.extend(child_paths)
    return redacted_list, paths


def _sensitive_extra_arg_indices(
    value: list[Any],
    path: tuple[str, ...],
) -> set[int]:
    if not path or path[-1] != "extra_args":
        return set()

    sensitive_indices: set[int] = set()
    previous_was_sensitive_split_flag = False
    for index, child in enumerate(value):
        if previous_was_sensitive_split_flag:
            sensitive_indices.add(index)
        previous_was_sensitive_split_flag = False
        if not isinstance(child, str):
            continue
        if _SENSITIVE_EXTRA_ARG_PATTERN.search(child) is None:
            continue
        if "=" in child:
            sensitive_indices.add(index)
            continue
        previous_was_sensitive_split_flag = True
    return sensitive_indices


def _path_label(path: tuple[str, ...]) -> str:
    return ".".join(path)


def _ensure_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("Runtime config payload must remain a dictionary.")
    return value

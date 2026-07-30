from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .json import JsonObject, JsonValue

SignatureScope = Literal["execution", "artifact", "observer", "validation"]

_SENSITIVE_OPTION_PATTERN = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|credential|private)",
    re.IGNORECASE,
)


class CanonicalIntegrationConfig(BaseModel):
    """Adapter-selected JSON options after side-effect-free canonicalization.

    External dotted-path adapters own option and capability schemas, so this
    boundary intentionally preserves JSON-compatible payloads.
    """

    model_config = ConfigDict(extra="forbid")

    implementation: str
    resolved_identity: str
    options: JsonObject = Field(default_factory=dict)
    sensitive_options: list[str] = Field(default_factory=list)
    option_fingerprints: list[dict[str, str]] = Field(default_factory=list)
    option_scopes: dict[str, SignatureScope] = Field(default_factory=dict)
    capabilities: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_option_metadata(self) -> CanonicalIntegrationConfig:
        _validate_finite_json(self.options, "options")
        _validate_finite_json(self.capabilities, "capabilities")
        option_keys = set(self.options)
        scope_keys = set(self.option_scopes)
        if option_keys != scope_keys:
            missing = sorted(option_keys - scope_keys)
            unknown = sorted(scope_keys - option_keys)
            details = []
            if missing:
                details.append(f"missing scopes for {missing}")
            if unknown:
                details.append(f"scopes without options {unknown}")
            raise ValueError(
                "Canonical integration option scopes must exactly match option keys: "
                + "; ".join(details)
            )
        unknown_sensitive = sorted(set(self.sensitive_options) - option_keys)
        if unknown_sensitive:
            raise ValueError(
                "Canonical integration sensitive options must name canonical options: "
                f"{unknown_sensitive}"
            )
        return self

    def scoped_payload(self, scopes: set[SignatureScope]) -> JsonObject:
        scoped_options: JsonObject = {
            key: value
            for key, value in self.options.items()
            if self.option_scopes.get(key) in scopes
        }
        payload: JsonObject = {
            "capabilities": self.capabilities,
            "implementation": self.implementation,
            "options": scoped_options,
            "resolved_identity": self.resolved_identity,
        }
        return payload

    def redacted_payload(self) -> JsonObject:
        sensitive_keys = sensitive_integration_option_keys(self)
        sensitive_options: list[JsonValue] = []
        sensitive_options.extend(sorted(sensitive_keys))
        redacted_options: JsonObject = {
            key: (
                redacted_integration_option_value(value)
                if key in sensitive_keys
                else value
            )
            for key, value in self.options.items()
        }
        payload: JsonObject = {
            "capabilities": self.capabilities,
            "implementation": self.implementation,
            "option_fingerprints": [dict(item) for item in self.option_fingerprints],
            "option_scopes": dict(self.option_scopes),
            "options": redacted_options,
            "resolved_identity": self.resolved_identity,
            "sensitive_options": sensitive_options,
        }
        return payload


def sensitive_integration_option_keys(
    config: CanonicalIntegrationConfig,
) -> set[str]:
    return {
        key
        for key in config.options
        if key in config.sensitive_options
        or _SENSITIVE_OPTION_PATTERN.search(key) is not None
    }


def redacted_integration_option_value(
    value: JsonValue,
    fingerprint: str | None = None,
    value_handle: str | None = None,
) -> JsonObject:
    redacted: JsonObject
    if _is_redacted_integration_option(value):
        if not isinstance(value, dict):
            raise AssertionError("redacted integration options must be mappings")
        redacted = dict(value)
    else:
        redacted = {"redacted": True}
    if fingerprint is not None:
        redacted["fingerprint"] = fingerprint
    if value_handle is not None:
        redacted["value_handle"] = value_handle
    return redacted


def _is_redacted_integration_option(value: JsonValue) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("redacted") is not True:
        return False
    return set(value) <= {"fingerprint", "redacted", "value_handle"}


def _validate_finite_json(value: JsonValue, path: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"Canonical integration value '{path}' must be finite")
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_finite_json(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_finite_json(child, f"{path}.{index}")

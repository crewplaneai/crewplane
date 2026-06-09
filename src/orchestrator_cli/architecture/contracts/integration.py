from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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

    def scoped_payload(self, scopes: set[SignatureScope]) -> JsonObject:
        scoped_options = {
            key: value
            for key, value in self.options.items()
            if self.option_scopes.get(key) in scopes
        }
        return {
            "capabilities": self.capabilities,
            "implementation": self.implementation,
            "options": scoped_options,
            "resolved_identity": self.resolved_identity,
        }

    def redacted_payload(self) -> JsonObject:
        sensitive_keys = sensitive_integration_option_keys(self)
        return {
            "capabilities": self.capabilities,
            "implementation": self.implementation,
            "option_fingerprints": self.option_fingerprints,
            "option_scopes": self.option_scopes,
            "options": {
                key: (
                    redacted_integration_option_value(value)
                    if key in sensitive_keys
                    else value
                )
                for key, value in self.options.items()
            },
            "resolved_identity": self.resolved_identity,
            "sensitive_options": sorted(sensitive_keys),
        }


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
    redacted = (
        dict(value) if _is_redacted_integration_option(value) else {"redacted": True}
    )
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

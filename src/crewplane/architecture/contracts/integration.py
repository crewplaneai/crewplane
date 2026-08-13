from __future__ import annotations

import math
from copy import deepcopy
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from .integration_secrets import (
    json_pointer,
    parse_json_pointer,
    transform_sensitive_integration_options,
    validate_sensitive_integration_option_pointers,
)
from .json import JsonObject, JsonValue

SignatureScope = Literal["execution", "artifact", "observer", "validation"]


class CanonicalIntegrationConfig(BaseModel):
    """Adapter-selected JSON options after side-effect-free canonicalization.

    External dotted-path adapters own option and capability schemas, so this
    boundary intentionally preserves JSON-compatible payloads.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    implementation: str
    resolved_identity: str
    options: JsonObject = Field(default_factory=dict, repr=False)
    sensitive_options: list[str] = Field(default_factory=list)
    option_fingerprints: list[dict[str, str]] = Field(
        default_factory=list,
        repr=False,
    )
    option_scopes: dict[str, SignatureScope] = Field(default_factory=dict)
    capabilities: JsonObject = Field(default_factory=dict)
    _options_are_generated_redacted: bool = PrivateAttr(default=False)

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
        validate_sensitive_integration_option_pointers(
            self.options,
            _declared_sensitive_option_pointers(self),
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
        option_fingerprints: list[JsonValue] = []
        if self._options_are_generated_redacted:
            redacted_options = deepcopy(self.options)
            sensitive_options = list(self.sensitive_options)
            option_fingerprints = [
                cast(JsonValue, dict(item)) for item in self.option_fingerprints
            ]
        else:
            redacted_options, sensitive_options = (
                transform_sensitive_integration_options(
                    self.options,
                    _declared_sensitive_option_pointers(self),
                    _redact_sensitive_option,
                )
            )
        payload: JsonObject = {
            "capabilities": self.capabilities,
            "implementation": self.implementation,
            "option_fingerprints": option_fingerprints,
            "option_scopes": dict(self.option_scopes),
            "options": redacted_options,
            "resolved_identity": self.resolved_identity,
            "sensitive_options": list(sensitive_options),
        }
        return payload

    def with_generated_redaction(
        self,
        options: JsonObject,
        sensitive_options: list[str],
        option_fingerprints: list[dict[str, str]],
    ) -> CanonicalIntegrationConfig:
        """Record redaction metadata produced by Crewplane's runtime boundary."""
        redacted = self.model_copy(
            update={
                "options": options,
                "option_fingerprints": option_fingerprints,
                "sensitive_options": sensitive_options,
            }
        )
        redacted._options_are_generated_redacted = True
        return redacted


def sensitive_integration_option_pointers(
    config: CanonicalIntegrationConfig,
) -> list[str]:
    _, pointers = transform_sensitive_integration_options(
        config.options,
        _declared_sensitive_option_pointers(config),
    )
    return pointers


def sensitive_integration_option_keys(
    config: CanonicalIntegrationConfig,
) -> set[str]:
    return {
        parse_json_pointer(pointer)[0]
        for pointer in sensitive_integration_option_pointers(config)
    }


def _declared_sensitive_option_pointers(
    config: CanonicalIntegrationConfig,
) -> list[str]:
    if config._options_are_generated_redacted:
        return list(config.sensitive_options)
    return [
        (
            json_pointer((declaration,))
            if declaration in config.options or not declaration.startswith("/")
            else declaration
        )
        for declaration in config.sensitive_options
    ]


def redacted_integration_option_value(
    value: JsonValue = None,
    fingerprint: str | None = None,
    value_handle: str | None = None,
) -> JsonObject:
    """Build trusted redaction metadata without retaining the raw value."""
    del value
    redacted: JsonObject = {"redacted": True}
    if fingerprint is not None:
        redacted["fingerprint"] = fingerprint
    if value_handle is not None:
        redacted["value_handle"] = value_handle
    return redacted


def _redact_sensitive_option(
    pointer: str,
    value: JsonValue,  # noqa: ARG001 - Required by the recursive transform callback.
) -> JsonValue:
    if not pointer:
        raise AssertionError("Sensitive integration option paths must not be empty.")
    return redacted_integration_option_value()


def _validate_finite_json(value: JsonValue, path: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"Canonical integration value '{path}' must be finite")
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_finite_json(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_finite_json(child, f"{path}.{index}")

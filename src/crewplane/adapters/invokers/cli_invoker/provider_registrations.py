"""Explicit, installed CLI capabilities and their signed execution identity."""

from __future__ import annotations

import importlib
import math
import re
from collections.abc import Mapping
from importlib import metadata
from types import MappingProxyType

from crewplane.architecture.contracts import (
    JsonObject,
    LogPresentationDescriptor,
    OneShotFailureRetryPolicy,
    ProviderKind,
)
from crewplane.core.config import validate_provider_identifier

from .capabilities import CAPABILITIES
from .capability import CliProviderCapability
from .discovery import CliProviderOnboarding

_BUILTIN_NAMES = frozenset(kind.value for kind in ProviderKind)
_REFERENCE = re.compile(
    r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", re.ASCII
)
_DISTRIBUTION = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", re.ASCII)


def canonicalize_provider_options(options: JsonObject | None) -> JsonObject:
    """Resolve authored registrations without invoking capability callbacks."""
    canonical: JsonObject = {}
    for identifier, raw in _registrations(options).items():
        record = _parse_record(identifier, raw, canonical=False)
        version = _installed_version(record)
        _load_capability(identifier, record["implementation"])
        canonical[identifier] = {**record, "version": version}
    return {"providers": canonical}


def verify_provider_options(
    options: JsonObject | None,
) -> Mapping[str, CliProviderCapability]:
    """Verify signed records; never refresh their identity or mutate built-ins."""
    providers: dict[str, CliProviderCapability] = dict(CAPABILITIES)
    for identifier, raw in _registrations(options).items():
        record = _parse_record(identifier, raw, canonical=True)
        installed_version = _installed_version(record)
        if installed_version != record["version"]:
            raise ValueError(
                f"CLI provider '{identifier}': distribution '{record['distribution']}' "
                f"version mismatch: expected {record['version']!r}, installed {installed_version!r}"
            )
        providers[identifier] = _load_capability(identifier, record["implementation"])
    return MappingProxyType(providers)


def _registrations(options: JsonObject | None) -> JsonObject:
    if options is None:
        return {}
    unknown = options.keys() - {"providers"}
    if unknown:
        raise ValueError(f"Unsupported cli invoker options: {sorted(unknown)}")
    registrations = options.get("providers", {})
    if not isinstance(registrations, dict):
        raise ValueError("cli invoker providers must be a mapping")
    return registrations


def _parse_record(identifier: str, raw: object, canonical: bool) -> dict[str, str]:
    validate_provider_identifier(identifier)
    if identifier in _BUILTIN_NAMES:
        raise ValueError(
            f"CLI provider '{identifier}' is a reserved built-in identifier"
        )
    fields = (
        {"implementation", "distribution", "version"}
        if canonical
        else {"implementation", "distribution"}
    )
    if not isinstance(raw, dict) or raw.keys() != fields:
        raise ValueError(
            f"CLI provider '{identifier}' requires exactly {sorted(fields)}"
        )
    record: dict[str, str] = {}
    for key in sorted(fields):
        value = raw[key]
        if not isinstance(value, str) or not value or value.strip() != value:
            raise ValueError(
                f"CLI provider '{identifier}': {key} must be a nonblank string"
            )
        record[key] = value
    if _REFERENCE.fullmatch(record["implementation"]) is None:
        raise ValueError(
            f"CLI provider '{identifier}': implementation must be module:object"
        )
    if _DISTRIBUTION.fullmatch(record["distribution"]) is None:
        raise ValueError(f"CLI provider '{identifier}': invalid distribution name")
    if canonical and any(char.isspace() for char in record["version"]):
        raise ValueError(f"CLI provider '{identifier}': invalid distribution version")
    return record


def _installed_version(record: Mapping[str, str]) -> str:
    declared = record["distribution"]
    try:
        installed = metadata.distribution(declared)
    except metadata.PackageNotFoundError as exc:
        raise ValueError(
            f"CLI provider distribution '{declared}' is not installed"
        ) from exc
    name = installed.metadata.get("Name")
    if not isinstance(name, str) or _normalized_distribution(
        name
    ) != _normalized_distribution(declared):
        raise ValueError(
            f"CLI provider distribution identity mismatch for '{declared}': {name!r}"
        )
    version = installed.metadata.get("Version")
    if (
        not isinstance(version, str)
        or not version
        or any(char.isspace() for char in version)
    ):
        raise ValueError(
            f"CLI provider distribution '{declared}' has invalid version metadata"
        )
    return version


def _normalized_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _load_capability(identifier: str, reference: str) -> CliProviderCapability:
    module_name, attribute_path = reference.split(":")
    try:
        value: object = importlib.import_module(module_name)
        for attribute in attribute_path.split("."):
            value = getattr(value, attribute)
    except Exception as exc:
        raise ValueError(
            f"CLI provider '{identifier}': cannot import '{reference}': {exc}"
        ) from exc
    if not isinstance(value, CliProviderCapability):
        raise ValueError(
            f"CLI provider '{identifier}': export must be a CliProviderCapability"
        )
    try:
        _validate_capability(identifier, value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"CLI provider '{identifier}': {exc}") from exc
    return value


def _validate_capability(identifier: str, capability: CliProviderCapability) -> None:
    validate_provider_identifier(capability.provider_kind)
    if capability.provider_kind != identifier:
        raise ValueError(f"capability identity mismatch: {capability.provider_kind!r}")
    for name in (
        "validate_request",
        "build_command",
        "quota_classifier",
        "failure_classifier",
    ):
        if not callable(getattr(capability, name)):
            raise ValueError(f"{name} must be callable")
    for name in ("output_extractor", "usage_decoder", "check_availability"):
        callback = getattr(capability, name)
        if callback is not None and not callable(callback):
            raise ValueError(f"{name} must be callable or None")
    LogPresentationDescriptor(
        capability.log_presentation_format, capability.log_presentation_profile
    )
    if not isinstance(capability.supports_output_idle_timeout, bool):
        raise ValueError("supports_output_idle_timeout must be a bool")
    if capability.one_shot_failure_retry is not None:
        _validate_retry_policy(capability.one_shot_failure_retry)
    if capability.onboarding is not None:
        if not isinstance(capability.onboarding, CliProviderOnboarding):
            raise ValueError("onboarding must be a CliProviderOnboarding or None")
        if not callable(capability.onboarding.discover) or not callable(
            capability.onboarding.list_models
        ):
            raise ValueError("onboarding callbacks must be callable")


def _validate_retry_policy(policy: OneShotFailureRetryPolicy) -> None:
    if not isinstance(policy, OneShotFailureRetryPolicy):
        raise ValueError(
            "one_shot_failure_retry must be a OneShotFailureRetryPolicy or None"
        )
    if (
        not isinstance(policy.output_contains, tuple)
        or not policy.output_contains
        or not all(
            isinstance(value, str) and value.strip() for value in policy.output_contains
        )
    ):
        raise ValueError(
            "one_shot_failure_retry.output_contains must be a nonempty tuple of nonblank strings"
        )
    if (
        isinstance(policy.wait_seconds, bool)
        or not isinstance(policy.wait_seconds, (int, float))
        or not math.isfinite(policy.wait_seconds)
        or policy.wait_seconds < 0
    ):
        raise ValueError(
            "one_shot_failure_retry.wait_seconds must be finite and non-negative"
        )
    for name in ("reason", "notice_message"):
        value = getattr(policy, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"one_shot_failure_retry.{name} must be a nonblank string")

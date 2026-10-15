import copy
import importlib
from dataclasses import replace
from unittest.mock import patch

import pytest

from crewplane.adapters.invokers.cli_invoker.capabilities import CAPABILITIES
from crewplane.adapters.invokers.cli_invoker.provider_registrations import (
    canonicalize_provider_options,
    verify_provider_options,
)
from crewplane.architecture.contracts import OneShotFailureRetryPolicy, ProviderKind
from tests.helpers.cli_provider import (
    FIXTURE_PACKAGE,
    expose_distribution,
    fixture_options,
)


@pytest.fixture
def registration(tmp_path, monkeypatch):
    metadata = expose_distribution(
        monkeypatch, tmp_path, FIXTURE_PACKAGE, "crewplane-fixture-provider", "1.2.3"
    )
    return fixture_options(), metadata


def test_canonicalization_and_verification_are_probe_free_and_immutable(
    registration, monkeypatch
):
    authored, _ = registration
    module = importlib.import_module("crewplane_fixture_provider")

    def unexpected(*args):
        raise AssertionError(f"callback executed: {args}")

    monkeypatch.setattr(
        module,
        "FIXTURE",
        replace(
            module.FIXTURE,
            validate_request=unexpected,
            build_command=unexpected,
            check_availability=unexpected,
        ),
    )
    before = dict(CAPABILITIES)
    canonical = canonicalize_provider_options(authored)
    assert canonical["providers"]["fixture"] == {
        **authored["providers"]["fixture"],
        "version": "1.2.3",
    }
    providers = verify_provider_options(canonical)
    assert providers["fixture"] is module.FIXTURE
    assert before == CAPABILITIES
    assert "fixture" not in verify_provider_options({})
    with pytest.raises(TypeError):
        providers["fixture"] = module.FIXTURE


@pytest.mark.parametrize("options", [None, {}, {"providers": {}}])
def test_empty_registrations_have_one_canonical_shape(options):
    assert canonicalize_provider_options(options) == {"providers": {}}
    assert dict(verify_provider_options(options)) == CAPABILITIES


def test_only_configured_modules_are_imported(registration):
    options, _ = registration
    with patch(
        "crewplane.adapters.invokers.cli_invoker.provider_registrations.importlib.import_module",
        wraps=importlib.import_module,
    ) as imported:
        canonical = canonicalize_provider_options(options)
        verify_provider_options(canonical)
    assert [call.args for call in imported.call_args_list] == [
        ("crewplane_fixture_provider",),
        ("crewplane_fixture_provider",),
    ]


@pytest.mark.parametrize("canonical", [False, True])
def test_failed_registration_maps_do_not_publish_partial_state(
    registration, monkeypatch, canonical
):
    authored, _ = registration
    records = canonicalize_provider_options(authored)
    selected = verify_provider_options(records)
    original = dict(selected)
    builtins = dict(CAPABILITIES)
    module = importlib.import_module("crewplane_fixture_provider")
    monkeypatch.setattr(module, "FIXTURE", module.ALTERNATE)
    options = copy.deepcopy(records if canonical else authored)
    options["providers"]["another"] = {
        **options["providers"]["fixture"],
        "implementation": "crewplane_fixture_provider:INVALID_EXPORT",
    }

    with pytest.raises(ValueError, match="CliProviderCapability"):
        (verify_provider_options if canonical else canonicalize_provider_options)(
            options
        )
    assert dict(selected) == original
    assert builtins == CAPABILITIES
    assert dict(verify_provider_options({})) == builtins


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize(
    "options",
    [
        {"other": 1},
        {"providers": []},
        {"providers": None},
        {"providers": {"bad.name": {}}},
        {"providers": {"Fixture": {}}},
        {"providers": {"fixture": None}},
        {"providers": {"fixture": {}}},
    ],
)
def test_malformed_options_are_rejected(options, canonical):
    with pytest.raises(ValueError):
        (verify_provider_options if canonical else canonicalize_provider_options)(
            options
        )


@pytest.mark.parametrize("identifier", list(ProviderKind))
def test_builtins_are_reserved(identifier):
    with pytest.raises(ValueError, match="reserved"):
        canonicalize_provider_options({"providers": {identifier: {}}})


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize(
    "field,value",
    [
        ("extra", "bad"),
        ("implementation", None),
        ("distribution", " "),
        ("implementation", "module"),
        ("implementation", "foo:bar()"),
        ("distribution", "bad/name"),
        ("distribution", 4),
    ],
)
def test_record_shapes_validate_each_boundary(registration, canonical, field, value):
    options, _ = registration
    if canonical:
        options = canonicalize_provider_options(options)
    options["providers"]["fixture"][field] = value
    with pytest.raises(ValueError, match="CLI provider"):
        (verify_provider_options if canonical else canonicalize_provider_options)(
            options
        )


def test_authored_version_is_rejected_and_canonical_version_is_required(registration):
    options, _ = registration
    with pytest.raises(ValueError, match="exactly"):
        verify_provider_options(options)
    options["providers"]["fixture"]["version"] = "1.2.3"
    with pytest.raises(ValueError, match="exactly"):
        canonicalize_provider_options(options)
    for version in ("", "1 2", None):
        options["providers"]["fixture"]["version"] = version
        with pytest.raises(ValueError):
            verify_provider_options(options)


@pytest.mark.parametrize(
    "reference,match",
    [
        ("unavailable_fixture:VALUE", "cannot import"),
        ("crewplane_fixture_provider:ABSENT", "cannot import"),
        ("crewplane_fixture_provider:INVALID_EXPORT", "CliProviderCapability"),
        ("crewplane_fixture_provider:INVALID_CALLBACK", "build_command"),
        ("crewplane_fixture_provider:WRONG_IDENTITY", "identity mismatch"),
    ],
)
def test_invalid_exports_fail_explicitly(registration, reference, match):
    options, _ = registration
    options["providers"]["fixture"]["implementation"] = reference
    with pytest.raises(ValueError, match=match):
        canonicalize_provider_options(options)
    assert "fixture" not in CAPABILITIES


@pytest.mark.parametrize(
    "field,value",
    [
        ("validate_request", None),
        ("quota_classifier", None),
        ("failure_classifier", 1),
        ("output_extractor", False),
        ("usage_decoder", "decoder"),
        ("check_availability", True),
        ("log_presentation_format", "html"),
        ("log_presentation_profile", ""),
        ("supports_output_idle_timeout", 1),
        ("one_shot_failure_retry", "retry"),
        (
            "one_shot_failure_retry",
            OneShotFailureRetryPolicy((), 1, "reason", "notice"),
        ),
        (
            "one_shot_failure_retry",
            OneShotFailureRetryPolicy(("retry",), float("inf"), "reason", "notice"),
        ),
        (
            "one_shot_failure_retry",
            OneShotFailureRetryPolicy(("retry",), 1, "", "notice"),
        ),
    ],
)
def test_invalid_capability_contracts(registration, monkeypatch, field, value):
    options, _ = registration
    module = importlib.import_module("crewplane_fixture_provider")
    monkeypatch.setattr(module, "FIXTURE", replace(module.FIXTURE, **{field: value}))
    with pytest.raises(
        ValueError, match=field.replace("_", " ") if field.startswith("log_") else field
    ):
        canonicalize_provider_options(options)


def test_metadata_and_signed_drift_are_rejected_without_refresh(registration):
    options, metadata = registration
    canonical = canonicalize_provider_options(options)
    signed = copy.deepcopy(canonical)
    metadata.write_text("Name: crewplane-fixture-provider\nVersion: 2.0\n")
    with pytest.raises(ValueError, match="version mismatch"):
        verify_provider_options(canonical)
    assert canonical == signed
    metadata.write_text("Name: wrong-distribution\nVersion: 1.2.3\n")
    with pytest.raises(ValueError, match="identity mismatch"):
        verify_provider_options(canonical)
    metadata.write_text("Name: crewplane-fixture-provider\n")
    with pytest.raises(ValueError, match="version metadata"):
        canonicalize_provider_options(options)
    options["providers"]["fixture"]["distribution"] = "absent-crewplane-fixture"
    with pytest.raises(ValueError, match="not installed"):
        canonicalize_provider_options(options)


def test_verification_detects_changed_capability_identity(registration, monkeypatch):
    options, _ = registration
    canonical = canonicalize_provider_options(options)
    module = importlib.import_module("crewplane_fixture_provider")
    monkeypatch.setattr(module, "FIXTURE", module.WRONG_IDENTITY)
    with pytest.raises(ValueError, match="identity mismatch"):
        verify_provider_options(canonical)

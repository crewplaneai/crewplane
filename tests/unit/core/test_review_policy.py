import pytest

from crewplane.core.review_policy import (
    audit_round_context,
    audit_rounds_or_default,
    maximum_review_round,
    remediation_depth_or_default,
)


@pytest.mark.parametrize(("value", "expected"), [(None, 1), (1, 1), (3, 3)])
def test_review_policy_defaults_preserve_explicit_values(value, expected):
    assert remediation_depth_or_default(value) == expected
    assert audit_rounds_or_default(value) == expected
    assert maximum_review_round(remediation_depth_or_default(value)) == expected + 1


@pytest.mark.parametrize(("depth", "expected"), [(0, 1), (1, 2), (3, 4)])
def test_review_rounds_include_the_initial_round(depth, expected):
    assert maximum_review_round(depth) == expected


@pytest.mark.parametrize(("audits", "expected"), [(None, None), (1, None), (3, 2)])
def test_audit_coordinates_are_optional_for_single_audits(audits, expected):
    assert audit_round_context(audits, 2) == expected

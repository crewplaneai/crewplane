import pytest
from pydantic import ValidationError

from crewplane.core.preflight.models import TokenBudgetPolicy
from crewplane.core.token_budget import (
    TokenBudgetOverride,
    TokenBudgetSettings,
    TokenBudgetThresholds,
    resolve_token_budget,
)


@pytest.mark.parametrize(
    "model", [TokenBudgetSettings, TokenBudgetOverride, TokenBudgetPolicy]
)
@pytest.mark.parametrize(
    ("warn", "fail", "valid"),
    [
        (10, 20, True),
        (10, 10, True),
        (None, 10, True),
        (10, None, True),
        (None, None, True),
        (20, 10, False),
        (0, 10, False),
        (10, 0, False),
        (-1, 10, False),
        (10, -1, False),
    ],
)
def test_budget_models_share_threshold_contract(
    model: type[TokenBudgetThresholds],
    warn: int | None,
    fail: int | None,
    valid: bool,
) -> None:
    if not valid:
        with pytest.raises(ValidationError):
            model(warn_threshold_chars=warn, fail_threshold_chars=fail)
        return
    budget = model(warn_threshold_chars=warn, fail_threshold_chars=fail)
    assert (budget.warn_threshold_chars, budget.fail_threshold_chars) == (warn, fail)


@pytest.mark.parametrize(
    "model", [TokenBudgetSettings, TokenBudgetOverride, TokenBudgetPolicy]
)
def test_budget_models_preserve_coercion_and_explicit_fields(
    model: type[TokenBudgetThresholds],
) -> None:
    budget = model.model_validate(
        {"warn_threshold_chars": "12", "fail_threshold_chars": 24.0}
    )

    assert budget.model_dump() == {
        "warn_threshold_chars": 12,
        "fail_threshold_chars": 24,
    }
    assert budget.model_fields_set == {
        "warn_threshold_chars",
        "fail_threshold_chars",
    }
    assert model().model_fields_set == set()
    assert model(warn_threshold_chars=None).model_fields_set == {"warn_threshold_chars"}


@pytest.mark.parametrize(
    "model", [TokenBudgetSettings, TokenBudgetOverride, TokenBudgetPolicy]
)
@pytest.mark.parametrize(
    ("field", "value", "error_type"),
    [
        ("warn_threshold_chars", "invalid", "int_parsing"),
        ("fail_threshold_chars", 1.5, "int_from_float"),
        ("warn_threshold_chars", [], "int_type"),
        ("unexpected", 10, "extra_forbidden"),
    ],
)
def test_budget_models_preserve_boundary_errors(
    model: type[TokenBudgetThresholds],
    field: str,
    value: object,
    error_type: str,
) -> None:
    payload = {"warn_threshold_chars": None, "fail_threshold_chars": None, field: value}

    with pytest.raises(ValidationError) as caught:
        model.model_validate(payload)

    assert [
        (error["loc"], error["type"], error["input"]) for error in caught.value.errors()
    ] == [((field,), error_type, value)]


def test_compiled_budget_defaults_are_nullable() -> None:
    assert TokenBudgetPolicy().model_dump() == {
        "warn_threshold_chars": None,
        "fail_threshold_chars": None,
    }


def test_budget_override_is_validated_after_inheriting_threshold() -> None:
    settings = TokenBudgetSettings(warn_threshold_chars=100, fail_threshold_chars=200)
    override = TokenBudgetOverride(fail_threshold_chars=50)

    with pytest.raises(ValueError, match="fail_threshold_chars must be greater"):
        resolve_token_budget(settings, override)


def test_budget_omission_and_explicit_null_remain_distinct() -> None:
    settings = TokenBudgetSettings(warn_threshold_chars=100, fail_threshold_chars=200)
    inherited = resolve_token_budget(settings, TokenBudgetOverride())
    disabled = resolve_token_budget(
        settings, TokenBudgetOverride(fail_threshold_chars=None)
    )

    assert inherited.fail_threshold_chars == 200
    assert disabled.fail_threshold_chars is None
    assert disabled.warn_threshold_chars == 100
    assert resolve_token_budget(None, None).warn_threshold_chars == 50_000

import pytest
from pydantic import ValidationError

from crewplane.core.preflight.models import TokenBudgetPolicy
from crewplane.core.token_budget import (
    TokenBudgetOverride,
    TokenBudgetSettings,
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
    ],
)
def test_budget_models_share_threshold_contract(
    model: type[TokenBudgetSettings]
    | type[TokenBudgetOverride]
    | type[TokenBudgetPolicy],
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

from __future__ import annotations

from dataclasses import dataclass
from typing import NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _BaseTokenBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warn_threshold_chars: int | None = Field(default=None, ge=1)
    fail_threshold_chars: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_threshold_order(self) -> _BaseTokenBudget:
        if (
            self.warn_threshold_chars is not None
            and self.fail_threshold_chars is not None
            and self.fail_threshold_chars < self.warn_threshold_chars
        ):
            raise ValueError(
                "fail_threshold_chars must be greater than or equal to "
                "warn_threshold_chars"
            )
        return self


class TokenBudgetSettings(_BaseTokenBudget):
    warn_threshold_chars: int | None = Field(default=50000, ge=1)
    fail_threshold_chars: int | None = Field(default=None, ge=1)


class TokenBudgetOverride(_BaseTokenBudget):
    pass


class TokenBudgetPayload(TypedDict):
    warn_threshold_chars: NotRequired[int | None]
    fail_threshold_chars: NotRequired[int | None]


@dataclass(frozen=True)
class ResolvedTokenBudget:
    warn_threshold_chars: int | None
    fail_threshold_chars: int | None

    def has_thresholds(self) -> bool:
        return (
            self.warn_threshold_chars is not None
            or self.fail_threshold_chars is not None
        )


def resolve_token_budget(
    settings_budget: TokenBudgetSettings | None,
    node_budget: TokenBudgetOverride | None,
) -> ResolvedTokenBudget:
    default_budget = settings_budget or TokenBudgetSettings()
    warn_threshold_chars = default_budget.warn_threshold_chars
    fail_threshold_chars = default_budget.fail_threshold_chars

    if node_budget is not None:
        if "warn_threshold_chars" in node_budget.model_fields_set:
            warn_threshold_chars = node_budget.warn_threshold_chars
        if "fail_threshold_chars" in node_budget.model_fields_set:
            fail_threshold_chars = node_budget.fail_threshold_chars

    resolved_budget = ResolvedTokenBudget(
        warn_threshold_chars=warn_threshold_chars,
        fail_threshold_chars=fail_threshold_chars,
    )
    _validate_resolved_budget(resolved_budget)
    return resolved_budget


def token_budget_payload_dict(token_budget: TokenBudgetOverride) -> TokenBudgetPayload:
    payload: TokenBudgetPayload = {}
    if "warn_threshold_chars" in token_budget.model_fields_set:
        payload["warn_threshold_chars"] = token_budget.warn_threshold_chars
    if "fail_threshold_chars" in token_budget.model_fields_set:
        payload["fail_threshold_chars"] = token_budget.fail_threshold_chars
    return payload


def _validate_resolved_budget(budget: ResolvedTokenBudget) -> None:
    if (
        budget.warn_threshold_chars is not None
        and budget.fail_threshold_chars is not None
        and budget.fail_threshold_chars < budget.warn_threshold_chars
    ):
        raise ValueError(
            "fail_threshold_chars must be greater than or equal to warn_threshold_chars"
        )

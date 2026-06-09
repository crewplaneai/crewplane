from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from orchestrator_cli.architecture.contracts import InvocationContext

_SELECTOR_STRING_KEYS = {"node_id", "task_id", "provider", "role"}
_SELECTOR_INT_KEYS = {"audit_round_num", "round_num"}
_SELECTOR_KEYS = _SELECTOR_STRING_KEYS | _SELECTOR_INT_KEYS


@dataclass(frozen=True)
class FailSelector:
    criteria: Mapping[str, str | int]

    def matches(self, context: InvocationContext | None) -> bool:
        if context is None:
            return False
        for key, expected in self.criteria.items():
            if getattr(context, key) != expected:
                return False
        return True

    def summary(self) -> str:
        return ", ".join(
            f"{key}={value}" for key, value in sorted(self.criteria.items())
        )


def _validate_and_build_selector(
    raw_selector: object, selector_index: int
) -> FailSelector:
    if not isinstance(raw_selector, dict):
        raise ValueError(
            "mock invoker option 'fail_when' selectors must be objects; "
            f"selector[{selector_index}] is {type(raw_selector).__name__}"
        )
    if not raw_selector:
        raise ValueError(
            "mock invoker option 'fail_when' selectors cannot be empty; "
            f"selector[{selector_index}] has no keys"
        )

    selector_label = f"selector[{selector_index}]"
    for raw_key in raw_selector:
        if not isinstance(raw_key, str):
            raise ValueError(
                "mock invoker option 'fail_when' selector keys must be strings; "
                f"{selector_label} has key {raw_key!r}"
            )

    unknown = sorted(set(raw_selector) - _SELECTOR_KEYS)
    if unknown:
        raise ValueError(
            "mock invoker option 'fail_when' selector contains unsupported keys: "
            f"{', '.join(unknown)}"
        )

    selector: dict[str, str | int] = {}
    for key, raw_value in raw_selector.items():
        if key in _SELECTOR_STRING_KEYS:
            if not isinstance(raw_value, str) or not raw_value.strip():
                raise ValueError(
                    "mock invoker option 'fail_when' selector key "
                    f"'{key}' must be a non-empty string"
                )
            selector[key] = raw_value
            continue
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            raise ValueError(
                "mock invoker option 'fail_when' selector key "
                f"'{key}' must be an integer"
            )
        selector[key] = raw_value

    return FailSelector(criteria=selector)


def validate_fail_selectors(value: object) -> tuple[FailSelector, ...]:
    if not isinstance(value, list):
        raise ValueError("mock invoker option 'fail_when' must be a list of selectors")
    return tuple(
        _validate_and_build_selector(raw_selector, index)
        for index, raw_selector in enumerate(value)
    )

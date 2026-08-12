from __future__ import annotations

from crewplane.architecture.contracts import (
    InvocationContext,
    JsonObject,
    MockInvokerFailSelector,
)
from crewplane.core.workflow.keywords import ProviderRole

_SELECTOR_STRING_KEYS = {"node_id", "task_id", "provider", "role"}
_SELECTOR_FIELDS = (
    "node_id",
    "task_id",
    "provider",
    "role",
    "audit_round_num",
    "round_num",
)
_SELECTOR_KEYS = frozenset(_SELECTOR_FIELDS)


def _validate_and_build_selector(
    raw_selector: object, selector_index: int
) -> MockInvokerFailSelector:
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

    role = _string_criterion(selector, "role")
    return MockInvokerFailSelector(
        node_id=_string_criterion(selector, "node_id"),
        task_id=_string_criterion(selector, "task_id"),
        provider=_string_criterion(selector, "provider"),
        role=ProviderRole(role) if role is not None else None,
        audit_round_num=_integer_criterion(selector, "audit_round_num"),
        round_num=_integer_criterion(selector, "round_num"),
    )


def _string_criterion(criteria: dict[str, str | int], key: str) -> str | None:
    value = criteria.get(key)
    return value if isinstance(value, str) else None


def _integer_criterion(criteria: dict[str, str | int], key: str) -> int | None:
    value = criteria.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def validate_fail_selectors(value: object) -> tuple[MockInvokerFailSelector, ...]:
    if not isinstance(value, list):
        raise ValueError("mock invoker option 'fail_when' must be a list of selectors")
    return tuple(
        _validate_and_build_selector(raw_selector, index)
        for index, raw_selector in enumerate(value)
    )


def selector_matches(
    selector: MockInvokerFailSelector,
    context: InvocationContext | None,
) -> bool:
    if context is None:
        return False
    criteria = (
        (selector.node_id, context.node_id),
        (selector.task_id, context.task_id),
        (selector.provider, context.provider),
        (selector.role, context.role),
        (selector.audit_round_num, context.audit_round_num),
        (selector.round_num, context.round_num),
    )
    return all(expected is None or actual == expected for expected, actual in criteria)


def selector_summary(selector: MockInvokerFailSelector) -> str:
    values = selector_to_json(selector)
    return ", ".join(
        f"{key}={value}" for key, value in sorted(values.items()) if value is not None
    )


def selector_to_json(selector: MockInvokerFailSelector) -> JsonObject:
    return {
        "node_id": selector.node_id,
        "task_id": selector.task_id,
        "provider": selector.provider,
        "role": selector.role,
        "audit_round_num": selector.audit_round_num,
        "round_num": selector.round_num,
    }

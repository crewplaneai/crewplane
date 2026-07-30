from __future__ import annotations

from collections.abc import Callable
from inspect import signature
from typing import Any, cast

from crewplane.architecture.contracts import (
    ObserverCapabilities,
    RuntimeObserver,
)

type Observer = RuntimeObserver


def validate_observer_contract(observer: Observer) -> Observer:
    try:
        capabilities = observer.capabilities
        stop_requested = observer.stop_requested
        lifecycle_methods = (
            ("start", observer.start, 1),
            ("on_snapshot", observer.on_snapshot, 2),
            ("stop", observer.stop, 1),
        )
    except AttributeError as exc:
        raise TypeError(
            f"Observer lifecycle contract is incomplete: {observer.__class__.__name__}"
        ) from exc
    if not isinstance(capabilities, ObserverCapabilities):
        raise TypeError(
            "Observer capabilities must be an ObserverCapabilities instance: "
            f"{observer.__class__.__name__}"
        )
    if not isinstance(stop_requested, bool):
        raise TypeError(
            f"Observer stop_requested must be bool: {observer.__class__.__name__}"
        )
    for method_name, lifecycle_method, argument_count in lifecycle_methods:
        if not callable(lifecycle_method):
            raise TypeError(
                f"Observer lifecycle contract is incomplete: "
                f"{observer.__class__.__name__}"
            )
        _validate_lifecycle_method_arity(
            observer,
            method_name,
            cast(Callable[..., Any], lifecycle_method),
            argument_count,
        )
    return observer


def _validate_lifecycle_method_arity(
    observer: Observer,
    method_name: str,
    lifecycle_method: Callable[..., Any],
    argument_count: int,
) -> None:
    try:
        signature(lifecycle_method).bind(*([object()] * argument_count))
    except (TypeError, ValueError) as exc:
        raise TypeError(
            "Observer lifecycle method has an incompatible signature: "
            f"{observer.__class__.__name__}.{method_name}"
        ) from exc

from __future__ import annotations

from typing import Any

from crewplane.architecture.contracts import (
    ObserverCapabilities,
)
from crewplane.observability import ObservabilityHub


class RequiredStopFailureObserver:
    capabilities = ObserverCapabilities(required=True)

    @property
    def stop_requested(self) -> bool:
        return False

    def start(self, context: object) -> None:
        del context

    def on_snapshot(self, event: object, snapshot: object) -> None:
        del event, snapshot

    def stop(self, result: object) -> None:
        del result
        raise RuntimeError("required observer stop failed")


class RequiredStopFailureHub(ObservabilityHub):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        observers = list(kwargs.pop("observers"))
        super().__init__(
            *args,
            observers=[*observers, RequiredStopFailureObserver()],
            **kwargs,
        )

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from crewplane.architecture.contracts import AgentInvoker, RuntimeObserver
from crewplane.architecture.ports.artifacts import ArtifactStorePort


@dataclass(frozen=True, init=False)
class UIRuntimePlan:
    """Immutable observer plan returned by a UI adapter."""

    observers: tuple[RuntimeObserver, ...]
    suppress_progress_output: bool

    def __init__(
        self,
        observers: Iterable[RuntimeObserver],
        suppress_progress_output: bool,
    ) -> None:
        """Freeze the observer iterable and progress-output preference."""
        object.__setattr__(self, "observers", tuple(observers))
        object.__setattr__(self, "suppress_progress_output", suppress_progress_output)


@dataclass(frozen=True, init=False)
class RuntimeComponents:
    """Fully wired runtime dependencies owned by the composition root."""

    artifact_store: ArtifactStorePort
    base_invoker: AgentInvoker
    observers: tuple[RuntimeObserver, ...]
    suppress_progress_output: bool

    def __init__(
        self,
        artifact_store: ArtifactStorePort,
        base_invoker: AgentInvoker,
        observers: Iterable[RuntimeObserver],
        suppress_progress_output: bool,
    ) -> None:
        """Freeze observers while retaining the selected invoker and store."""
        object.__setattr__(self, "artifact_store", artifact_store)
        object.__setattr__(self, "base_invoker", base_invoker)
        object.__setattr__(self, "observers", tuple(observers))
        object.__setattr__(self, "suppress_progress_output", suppress_progress_output)

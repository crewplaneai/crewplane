from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from orchestrator_cli.architecture.ports.artifacts import ArtifactStorePort
from orchestrator_cli.observability.observer import Observer
from orchestrator_cli.runtime.agent.types import AgentInvoker


@dataclass(frozen=True, init=False)
class UIRuntimePlan:
    observers: tuple[Observer, ...]
    suppress_progress_output: bool

    def __init__(
        self,
        observers: Iterable[Observer],
        suppress_progress_output: bool,
    ) -> None:
        object.__setattr__(self, "observers", tuple(observers))
        object.__setattr__(self, "suppress_progress_output", suppress_progress_output)


@dataclass(frozen=True, init=False)
class RuntimeComponents:
    artifact_store: ArtifactStorePort
    base_invoker: AgentInvoker
    observers: tuple[Observer, ...]
    suppress_progress_output: bool

    def __init__(
        self,
        artifact_store: ArtifactStorePort,
        base_invoker: AgentInvoker,
        observers: Iterable[Observer],
        suppress_progress_output: bool,
    ) -> None:
        object.__setattr__(self, "artifact_store", artifact_store)
        object.__setattr__(self, "base_invoker", base_invoker)
        object.__setattr__(self, "observers", tuple(observers))
        object.__setattr__(self, "suppress_progress_output", suppress_progress_output)

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol


@dataclass(frozen=True)
class TopologyProvider:
    """Provider metadata needed for observer display."""

    provider: str
    model: str | None = None
    role: str | None = None


@dataclass(frozen=True)
class TopologyNode:
    """Plan-derived node metadata needed for observer display."""

    id: str
    mode: str
    dependencies: tuple[str, ...] = ()
    providers: tuple[TopologyProvider, ...] = ()


@dataclass(frozen=True)
class WorkflowTopology:
    """Narrow observer view of the compiled workflow DAG."""

    workflow_name: str
    nodes: tuple[TopologyNode, ...]

    @property
    def node_order(self) -> Mapping[str, int]:
        return MappingProxyType(
            {node.id: index for index, node in enumerate(self.nodes)}
        )


class Observer(Protocol):
    """Observer sink contract without importing concrete observability internals."""

    def start(self, context: object) -> None: ...

    def on_snapshot(self, event: object | None, snapshot: object) -> None: ...

    def stop(self, result: object) -> None: ...

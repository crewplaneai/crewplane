from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from crewplane.core.workflow.keywords import ProviderRole

from .json import JsonObject

MockOutputMode = Literal["lorem", "echo", "file"]


@dataclass(frozen=True)
class CliInvokerOptions:
    """The built-in CLI invoker has no user-configurable options."""


@dataclass(frozen=True)
class MockInvokerFailSelector:
    """Optional invocation fields that must all match to force a mock failure."""

    node_id: str | None = None
    task_id: str | None = None
    provider: str | None = None
    role: ProviderRole | None = None
    audit_round_num: int | None = None
    round_num: int | None = None

    def __post_init__(self) -> None:
        if self.role is not None:
            object.__setattr__(self, "role", ProviderRole(self.role))


@dataclass(frozen=True)
class MockInvokerOptions:
    """Canonical options consumed by the deterministic mock invoker."""

    delay_seconds: float = 0.0
    observation_delay_seconds: float = 5.0
    output_mode: MockOutputMode = "lorem"
    output_dir: str | None = None
    strict_file_mode: bool = False
    seed: int | None = None
    fail_when: tuple[MockInvokerFailSelector, ...] = ()
    fixture_metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class FilesystemArtifactOptions:
    """Canonical filesystem artifact-store behavior."""

    log_cli_output: bool = True


@dataclass(frozen=True)
class TmuxUiOptions:
    """Canonical settings for the observer-only tmux dashboard."""

    auto_close_session: bool = True
    tmux_executable: str = "tmux"
    quiet_after_seconds: float = 120.0
    log_tail_lines: int | None = None


@dataclass(frozen=True)
class NullUiOptions:
    """The built-in no-live UI adapter has no user-configurable options."""

from __future__ import annotations

from dataclasses import dataclass, field

from .json import JsonObject


@dataclass(frozen=True)
class CliInvokerOptions:
    """The built-in CLI invoker has no user-configurable options."""


@dataclass(frozen=True)
class MockInvokerFailSelector:
    node_id: str | None = None
    task_id: str | None = None
    provider: str | None = None
    role: str | None = None
    audit_round_num: int | None = None
    round_num: int | None = None


@dataclass(frozen=True)
class MockInvokerOptions:
    delay_seconds: float = 0.0
    observation_delay_seconds: float = 5.0
    output_mode: str = "lorem"
    output_dir: str | None = None
    strict_file_mode: bool = False
    seed: int | None = None
    fail_when: tuple[MockInvokerFailSelector, ...] = ()
    fixture_metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class FilesystemArtifactOptions:
    log_cli_output: bool = True
    allowed_template_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class TmuxUiOptions:
    auto_close_session: bool = True
    tmux_executable: str = "tmux"
    quiet_after_seconds: float = 120.0
    log_tail_lines: int | None = None


@dataclass(frozen=True)
class NullUiOptions:
    """The built-in no-live UI adapter has no user-configurable options."""

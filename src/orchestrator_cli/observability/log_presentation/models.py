from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from orchestrator_cli.architecture.contracts import LogPresentationFormat
from orchestrator_cli.observability.events.types import InvocationStatus

from .limits import LogPresentationLimits

NoticeLevel = Literal["info", "warning", "error"]


@dataclass(frozen=True)
class LogPresentationNotice:
    level: NoticeLevel
    message: str


@dataclass(frozen=True)
class LogPresentationSnapshot:
    size_bytes: int
    updated_age_seconds: float | None
    lines: tuple[str, ...]
    notices: tuple[LogPresentationNotice, ...] = ()
    truncated: bool = False


@dataclass(frozen=True)
class LogPresentationRequest:
    log_path: Path
    presentation_format: LogPresentationFormat
    presentation_profile: str
    line_budget: int
    invocation_status: InvocationStatus
    wall_time_now: float
    limits: LogPresentationLimits


@dataclass(frozen=True)
class LogReadResult:
    size_bytes: int
    updated_age_seconds: float | None
    body: bytes
    truncated: bool
    started_mid_line: bool = False

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PreflightDiagnosticSeverity = Literal["error", "warning"]


class PreflightDiagnostic(BaseModel):
    """A deterministic preflight diagnostic safe to persist."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    severity: PreflightDiagnosticSeverity = "error"
    phase: str
    node_id: str | None = None
    path: str | None = None
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)

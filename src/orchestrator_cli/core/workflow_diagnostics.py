from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

WorkflowDiagnosticSeverity = Literal["error", "warning"]

_NODE_MESSAGE_PATTERNS = (
    re.compile(r"^(?:Node|Sequential node|Parallel node|Input node) '([^']+)'"),
    re.compile(r"^Workflow input '[^']+' references unknown node '([^']+)'"),
)


@dataclass(frozen=True)
class WorkflowValidationDiagnostic:
    """A structured workflow validation diagnostic for public and preflight use."""

    code: str
    phase: str
    message: str
    severity: WorkflowDiagnosticSeverity = "error"
    node_id: str | None = None
    metadata: dict[str, str | int | bool | None] = field(default_factory=dict)


def node_id_from_message(message: str) -> str | None:
    for pattern in _NODE_MESSAGE_PATTERNS:
        match = pattern.search(message)
        if match is not None:
            return match.group(1)
    return None


def format_diagnostics(
    diagnostics: tuple[WorkflowValidationDiagnostic, ...],
) -> str:
    return "\n".join(diagnostic.message for diagnostic in diagnostics)

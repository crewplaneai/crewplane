from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orchestrator_cli.architecture.contracts import InvocationContext
from orchestrator_cli.core.review_contract import render_no_findings_review_contract


@dataclass(frozen=True)
class OutputResolution:
    content: str
    source: str
    fixture_path: Path | None = None


def build_findings_lines(context: InvocationContext | None) -> list[str]:
    if context is None or not context.findings_enabled:
        return []
    return [
        "",
        "<!-- findings -->",
        (
            f"- Synthetic finding for {context.node_id}: "
            f"{context.provider}/{context.role} mock output is ready."
        ),
        "<!-- /findings -->",
    ]


def review_contract_resolution(source: str) -> OutputResolution:
    return OutputResolution(
        content=build_no_findings_review_contract(),
        source=source,
    )


def build_no_findings_review_contract() -> str:
    return render_no_findings_review_contract()

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from crewplane.architecture.contracts import InvocationContext
from crewplane.core.review_contract import render_no_findings_review_contract

from .context import ContextDisplay, context_display


@dataclass(frozen=True)
class OutputResolution:
    content: str
    source: str
    fixture_path: Path | None = None


def build_lorem_markdown(
    prompt: str,
    context: InvocationContext | None,
    seed: int | None,
) -> str:
    display = context_display(context)
    marker = _seed_marker(display, seed)

    lines = [
        "# Mock Invocation Output",
        "",
        f"- Node: {display.node_id}",
        f"- Task: {display.task_id}",
        f"- Provider: {display.provider}",
        f"- Role: {display.role_display}",
        f"- Audit Round: {display.audit_round_display}",
        f"- Round: {display.round_display}",
    ]
    if seed is not None:
        lines.append(f"- Seed Marker: {marker}")
    lines.extend(
        [
            "",
            "## Summary",
            "Synthetic output generated for deterministic local orchestration checks.",
            "",
            "## Notes",
            f"- Prompt length: {len(prompt)} characters",
            "- Behavior path: mock invoker lorem mode",
            "",
            "## Next Steps",
            "1. Verify downstream template substitution.",
            "2. Validate node and invocation state transitions.",
        ]
    )
    lines.extend(build_findings_lines(context))
    return "\n".join(lines) + "\n"


def _seed_marker(display: ContextDisplay, seed: int | None) -> str:
    seed_value = seed if seed is not None else "no-seed"
    digest = hashlib.sha256(
        (
            f"{seed_value}|{display.node_id}|{display.task_id}|{display.provider}|"
            f"{display.role_display}|{display.audit_round_display}|"
            f"{display.round_display}"
        ).encode()
    ).hexdigest()
    return digest[:12]


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

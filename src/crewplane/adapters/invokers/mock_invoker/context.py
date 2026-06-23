from __future__ import annotations

from dataclasses import dataclass

from crewplane.architecture.contracts import InvocationContext
from crewplane.core.workflow.keywords import ProviderRole


@dataclass(frozen=True)
class ContextDisplay:
    node_id: str
    task_id: str
    provider: str
    role: str
    audit_round_display: str
    round_display: str


def _or_na(value: int | None) -> str:
    return str(value) if value is not None else "n/a"


def context_display(context: InvocationContext | None) -> ContextDisplay:
    if context is None:
        return ContextDisplay(
            node_id="<unknown>",
            task_id="<unknown>",
            provider="<unknown>",
            role="<unknown>",
            audit_round_display=_or_na(None),
            round_display=_or_na(None),
        )
    return ContextDisplay(
        node_id=context.node_id,
        task_id=context.task_id,
        provider=context.provider,
        role=context.role,
        audit_round_display=_or_na(context.audit_round_num),
        round_display=_or_na(context.round_num),
    )


def is_reviewer_context(context: InvocationContext | None) -> bool:
    return context is not None and context.role == ProviderRole.REVIEWER

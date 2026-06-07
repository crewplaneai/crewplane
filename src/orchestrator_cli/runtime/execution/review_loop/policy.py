from __future__ import annotations

from pathlib import Path

from orchestrator_cli.core.preflight.models import (
    PreflightExecutionNode,
    ProviderRecord,
)

from .types import (
    DEFAULT_AUDIT_ROUNDS,
    DEFAULT_REMEDIATION_DEPTH,
    AuditRoundResult,
    ReviewLoopRunContext,
)


def resolve_remediation_depth(node: PreflightExecutionNode) -> int:
    depth = node.execution_policy.depth
    if depth is not None and depth <= 0:
        raise ValueError(
            f"Sequential node '{node.id}' depth must be greater than 0 when provided."
        )
    return depth or DEFAULT_REMEDIATION_DEPTH


def resolve_audit_rounds(node: PreflightExecutionNode) -> int:
    audit_rounds = node.execution_policy.audit_rounds
    if audit_rounds is not None and audit_rounds <= 0:
        raise ValueError(
            f"Sequential node '{node.id}' audit_rounds must be greater than 0 when provided."
        )
    return audit_rounds or DEFAULT_AUDIT_ROUNDS


def audit_round_context(audit_rounds: int, audit_round_num: int) -> int | None:
    return audit_round_num if audit_rounds > 1 else None


def audit_round_dir(node_dir: Path, audit_rounds: int, audit_round_num: int) -> Path:
    if audit_rounds == 1:
        return node_dir
    review_dir = node_dir / f"review-audit-round-{audit_round_num}"
    review_dir.mkdir(parents=True, exist_ok=True)
    return review_dir


def split_sequential_review_loop_providers(
    node_id: str,
    providers: list[ProviderRecord],
) -> tuple[list[ProviderRecord], list[ProviderRecord]]:
    executors: list[ProviderRecord] = []
    reviewers: list[ProviderRecord] = []
    reviewer_segment_started = False

    for provider in providers:
        if provider.role == "reviewer":
            reviewer_segment_started = True
            reviewers.append(provider)
            continue
        if reviewer_segment_started:
            raise ValueError(
                f"Sequential node '{node_id}' must declare providers as a contiguous "
                "executor segment followed by a contiguous reviewer segment."
            )
        executors.append(provider)

    if not executors or not reviewers:
        raise ValueError(
            f"Sequential node '{node_id}' requires at least one executor and one reviewer."
        )
    return executors, reviewers


def consensus_failure_allows_continuation(
    node: PreflightExecutionNode,
    sequential_consensus_on_exhaustion: str,
) -> tuple[bool, str]:
    if node.execution_policy.continue_on_failure:
        return True, "continue_on_failure=true"
    if sequential_consensus_on_exhaustion == "continue":
        return True, "settings.sequential_consensus_on_exhaustion=continue"
    return False, "settings.sequential_consensus_on_exhaustion=fatal"


def review_loop_can_finish(
    context: ReviewLoopRunContext,
    audit_result: AuditRoundResult,
    audit_round_num: int,
) -> bool:
    if audit_result.clean_fresh_approval:
        return True
    if audit_result.consensus_reached and audit_round_num < context.audit_rounds:
        return False
    return audit_result.consensus_reached

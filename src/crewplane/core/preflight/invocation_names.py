"""Check invocation artifact components before execution or persisted-plan reuse."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from crewplane.architecture.contracts.artifacts import (
    MAX_ARTIFACT_PATH_COMPONENT_CHARS,
    build_log_filename,
    build_review_audit_directory_name,
    build_reviewer_state_filename,
    build_task_round_filename,
    candidate_identity_path,
    review_metadata_path,
    review_raw_output_path,
)
from crewplane.core.review_policy import (
    audit_rounds_or_default,
    maximum_review_round,
    remediation_depth_or_default,
)
from crewplane.core.workflow.keywords import ProviderRole

if TYPE_CHECKING:
    from .compiled_records import PreflightExecutionNode, ProviderRecord


def validate_invocation_names(node: PreflightExecutionNode) -> None:
    review_loop = node.mode == "sequential" and any(
        record.role == ProviderRole.REVIEWER for record in node.provider_records
    )
    policy = node.execution_policy
    maximum_round = (
        maximum_review_round(remediation_depth_or_default(policy.depth))
        if review_loop
        else 1
        if node.mode == "parallel"
        else policy.single_provider_total_rounds
    )
    audits = audit_rounds_or_default(policy.audit_rounds) if review_loop else 1
    for provider in node.provider_records:
        for round_num in {0, maximum_round}:
            names = _invocation_components(provider, round_num, audits, review_loop)
            for name in names:
                if len(name) > MAX_ARTIFACT_PATH_COMPONENT_CHARS:
                    raise ValueError(
                        f"Node '{node.id}' provider '{provider.provider}' generates "
                        f"component '{name}' exceeding the "
                        f"{MAX_ARTIFACT_PATH_COMPONENT_CHARS}-character budget; "
                        "shorten the provider alias."
                    )


def _invocation_components(
    provider: ProviderRecord, round_num: int, audits: int, review_loop: bool
) -> list[str]:
    task = Path(build_task_round_filename(provider.task_id, round_num))
    names = [
        task.name,
        build_log_filename(provider.task_id, audits if audits > 1 else None, round_num),
    ]
    if review_loop:
        if audits > 1:
            names.append(build_review_audit_directory_name(audits))
        if provider.role == ProviderRole.REVIEWER:
            names.extend(
                [
                    review_metadata_path(task).name,
                    review_raw_output_path(task).name,
                    build_reviewer_state_filename(provider.task_id, round_num),
                ]
            )
        else:
            names.append(candidate_identity_path(task).name)
    return names

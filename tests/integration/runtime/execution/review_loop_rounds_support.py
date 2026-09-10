from crewplane.core.preflight.models import (
    ArtifactContract,
    ExecutionPolicy,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    ProviderRecord,
    RenderPlan,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.failures import (
    FailureKind,
    FailurePhase,
    InvocationFailureError,
    InvocationFailureSummary,
)
from crewplane.runtime.execution.common import CompiledRuntimeContext
from crewplane.runtime.execution.consensus import (
    ParsedReviewResult,
    render_review_contract,
)
from crewplane.version import SCHEMA_VERSION


def make_round_runtime_context() -> CompiledRuntimeContext:
    return CompiledRuntimeContext(
        plan=PreflightExecutionPlan(
            plan_schema_version=SCHEMA_VERSION,
            run_id="run-1",
            run_key_name="run-1",
            project_root=".",
            context_root=".",
            manifest_root=".crewplane",
            created_at="2026-06-03T00:00:00",
            workflow_name="workflow",
            workflow_signature="workflow-signature",
            execution_order=["review.node"],
            nodes=[make_review_node()],
            render_plans=[
                RenderPlan(render_plan_id="review.node", node_id="review.node")
            ],
            static_resources=[],
            token_catalog=[],
            dependency_graph=[],
            runtime_config_snapshot={
                "execution": {"sequential_consensus_on_exhaustion": "continue"},
                "schema_version": SCHEMA_VERSION,
            },
            effective_runtime_config_signature="runtime-signature",
            fingerprint_metadata={"payload_version": "1"},
        ),
        secret_context=SecretContext(),
    )


def provider(provider: str, role: ProviderRole, task_id: str) -> ProviderRecord:
    return ProviderRecord(
        provider=provider,
        role=role,
        task_id=task_id,
        agent_config_key=provider,
        invoker_alias="mock",
        agent_config_signature=f"{provider}-agent",
        invoker_config_signature="mock-config",
    )


def make_review_node() -> PreflightExecutionNode:
    return PreflightExecutionNode(
        id="review.node",
        mode="sequential",
        render_plan_id="review.node",
        execution_policy=ExecutionPolicy(consensus_on_exhaustion="continue"),
        provider_records=[
            provider("exec", ProviderRole.EXECUTOR, "exec_executor_0"),
            provider("review", ProviderRole.REVIEWER, "review_reviewer_0"),
        ],
        artifact_contract=ArtifactContract(
            stage_path="review.node",
            output_path="review.node-result.md",
            log_path="review.node/logs",
            result_path="review.node-result.md",
        ),
    )


def review_output(verdict: str = "NO_FINDINGS", major: str = "None") -> str:
    return render_review_contract(
        ParsedReviewResult(
            verdict=verdict,
            major_issues=major,
            minor_issues="None",
            nitpicks="None",
        )
    )


def provider_failure(kind: FailureKind, phase: FailurePhase) -> InvocationFailureError:
    return InvocationFailureError(
        "simulated provider failure",
        InvocationFailureSummary(
            kind=kind,
            phase=phase,
            source="stdout_json",
            message=f"simulated {kind}",
            advice="test advice",
            condensed=False,
        ),
        None,
    )

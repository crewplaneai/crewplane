import sys
from io import BytesIO
from pathlib import Path

from crewplane.adapters.invokers.cli_invoker import (
    build_cli_invocation_plan,
    build_cli_log_presentation,
)
from crewplane.artifacts import OutputManager
from crewplane.core.config import AgentConfig
from crewplane.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    ProviderRecord,
    RenderPlan,
)
from crewplane.core.preflight.runtime_config import (
    RuntimeAgentConfigSnapshot,
    runtime_agent_signature_payload,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.preflight.signatures import signature_for_payload
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.invoker import PlannedAgentInvoker
from crewplane.runtime.execution.common import (
    CompiledRuntimeContext,
    ProviderCallDisplay,
)
from crewplane.runtime.execution.publication_registry import (
    RuntimePublicationRegistry,
)
from crewplane.runtime.execution.review_loop.types import (
    DriftGuardCallRequest,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.artifacts import node_artifact_request


def recovery_payload(
    registry: RuntimePublicationRegistry,
    path: Path,
) -> bytes | None:
    destination = BytesIO()
    if not registry.copy_recovery_payload_to(path, destination):
        return None
    return destination.getvalue()


def make_drift_request(
    tmp_path: Path,
    use_cli_invoker: bool = False,
) -> tuple[DriftGuardCallRequest, OutputManager, Path]:
    output = OutputManager("workflow", base_dir=tmp_path)
    agent_config = AgentConfig(
        cli_cmd=(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdin.read(); print('provider output')",
            ]
            if use_cli_invoker
            else ["mock"]
        ),
        default_model=None if use_cli_invoker else "m1",
    )
    agent_payload = agent_config.model_dump(mode="json", exclude_none=True)
    invoker_alias = "cli" if use_cli_invoker else "mock"
    invoker_payload = {
        "capabilities": {},
        "implementation": invoker_alias,
        "options": {},
        "resolved_identity": invoker_alias,
    }
    agent_signature = drift_agent_signature("exec", agent_payload, None)
    node = PreflightExecutionNode(
        id="review.node",
        mode="sequential",
        render_plan_id="review.node",
        provider_records=[
            ProviderRecord(
                provider="exec",
                role=ProviderRole.EXECUTOR,
                task_id="exec_executor_0",
                agent_config_key="exec",
                invoker_alias=invoker_alias,
                agent_config_signature=agent_signature,
                invoker_config_signature=signature_for_payload(invoker_payload),
            )
        ],
        artifact_contract=ArtifactContract(
            stage_path="review.node",
            output_path="review.node-result.md",
            log_path="review.node/logs",
            result_path="review.node-result.md",
        ),
    )
    node_dir = output.create_node_dir(node_artifact_request(node.id))
    request = DriftGuardCallRequest(
        runtime_context=CompiledRuntimeContext(
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
                nodes=[node],
                render_plans=[
                    RenderPlan(
                        render_plan_id="review.node",
                        node_id="review.node",
                    )
                ],
                static_resources=[],
                token_catalog=[],
                dependency_graph=[],
                runtime_config_snapshot={
                    "agents": {"exec": agent_payload},
                    "execution": {},
                    "invoker": {**invoker_payload, "option_scopes": {}},
                    "schema_version": SCHEMA_VERSION,
                },
                effective_runtime_config_signature="runtime-signature",
                fingerprint_metadata={"payload_version": "1"},
            ),
            secret_context=SecretContext(),
        ),
        output=output,
        node=node,
        node_dir=node_dir,
        invoker=(
            PlannedAgentInvoker(
                plan_builder=build_cli_invocation_plan,
                log_presentation_builder=build_cli_log_presentation,
            )
            if use_cli_invoker
            else object()
        ),
        telemetry=None,
        audit_round_num=None,
        round_num=1,
        provider=ProviderRecord(
            provider="exec",
            role=ProviderRole.EXECUTOR,
            task_id="exec_executor_0",
            agent_config_key="exec",
            invoker_alias=invoker_alias,
            agent_config_signature=agent_signature,
            invoker_config_signature=signature_for_payload(invoker_payload),
        ),
        task_id="exec_executor_0",
        prompt="Prompt",
        output_file=node_dir / "exec_executor_0_round1.md",
        role_label=ProviderRole.EXECUTOR,
        findings_enabled=False,
        allowed_paths=set(),
        display=ProviderCallDisplay(
            telemetry=None,
            progress_description="Executing exec...",
        ),
    )
    return request, output, node_dir


def drift_agent_signature(
    agent_config_key: str,
    agent_payload: object,
    resolved_model: str | None,
) -> str:
    agent_snapshot = RuntimeAgentConfigSnapshot.model_validate(agent_payload)
    return signature_for_payload(
        runtime_agent_signature_payload(
            agent_config_key,
            agent_snapshot,
            resolved_model,
        )
    )

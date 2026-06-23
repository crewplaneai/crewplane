import tempfile
import unittest
from pathlib import Path

from crewplane.adapters.invokers.mock import MockInvokerAdapter
from crewplane.artifacts import OutputManager
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.preflight import (
    DependencyEdge,
    Fragment,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    ProviderRecord,
    RenderPlan,
    RenderStream,
    signature_for_payload,
)
from crewplane.core.preflight.models import ArtifactContract
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability.events import ExecutionEvent
from crewplane.runtime.execution import (
    execute_workflow as _execute_compiled_workflow,
)
from crewplane.runtime.execution.consensus import (
    extract_verdict,
)
from crewplane.version import SCHEMA_VERSION
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    GraphDependencyOrderInvoker,
    MockAgentInvoker,
    execute_workflow,
)


class WorkflowDagSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_workflow_succeeds_with_mock_invoker_reviewer_outputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["echo"], default_model="exec-model"),
                    "review": AgentConfig(
                        cli_cmd=["echo"],
                        default_model="review-model",
                    ),
                },
            )
            workflow = WorkflowPlan(
                name="mock.review.loop",
                nodes=[
                    WorkflowNode(
                        id="implement.review",
                        mode="sequential",
                        prompt_segments=[
                            PromptSegment(
                                role="shared", content="Review implementation."
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                            ProviderSpec(provider="review", role=ProviderRole.REVIEWER),
                        ],
                    ),
                    WorkflowNode(
                        id="implement.handoff",
                        mode="parallel",
                        prompt_segments=[
                            PromptSegment(
                                role="shared", content="Hand off the final result."
                            )
                        ],
                        needs=["implement.review"],
                        providers=[
                            ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR)
                        ],
                    ),
                ],
            )
            invoker = MockInvokerAdapter().create_invoker(
                config=config,
                options={
                    "observation_delay_seconds": 0,
                    "output_mode": "echo",
                },
            )
            output = OutputManager(workflow.name, base_dir=tmp_path)

            await execute_workflow(config, workflow, output, invoker=invoker)

            reviewer_output = (
                output.stages_dir / "implement.review" / "review_reviewer_0_round1.md"
            ).read_text(encoding="utf-8")
            self.assertEqual(extract_verdict(reviewer_output), "NO_FINDINGS")
            self.assertTrue(output.get_stage_output_path("implement.handoff").exists())

    async def test_summary_node_waits_for_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="alpha"),
                    "beta": AgentConfig(cli_cmd=["mock"], default_model="beta"),
                    "gamma": AgentConfig(cli_cmd=["mock"], default_model="gamma"),
                },
            )
            workflow = WorkflowPlan(
                name="dag.workflow",
                nodes=[
                    WorkflowNode(
                        id="backend.auth",
                        mode="sequential",
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED, content="auth work"
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                        ],
                    ),
                    WorkflowNode(
                        id="backend.billing",
                        mode="sequential",
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED, content="billing work"
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="beta", role=ProviderRole.EXECUTOR)
                        ],
                    ),
                    WorkflowNode(
                        id="summary.final",
                        mode="sequential",
                        needs=["backend.auth", "backend.billing"],
                        prompt_segments=[
                            PromptSegment(
                                role="shared",
                                content="summary\n{{backend.auth.output}}\n{{backend.billing.output}}",
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="gamma", role=ProviderRole.EXECUTOR)
                        ],
                    ),
                ],
            )
            invoker = MockAgentInvoker(
                outputs=["auth-out", "billing-out", "summary-out"]
            )
            output = OutputManager(workflow.name, base_dir=tmp_path)

            await execute_workflow(config, workflow, output, invoker=invoker)

            summary_call = invoker.calls[2]
            self.assertIn("auth-out", summary_call["prompt"])
            self.assertIn("billing-out", summary_call["prompt"])
            self.assertEqual(len(invoker.calls), 3)

    async def test_scheduler_uses_compiled_dependency_graph_not_node_dependencies(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output = OutputManager("compiled.graph", base_dir=tmp_path)
            agent_config = AgentConfig(cli_cmd=["mock"], default_model="alpha")
            agent_payload = agent_config.model_dump(mode="json", exclude_none=True)
            invoker_payload = {
                "capabilities": {},
                "implementation": "mock",
                "options": {},
                "resolved_identity": "mock",
            }
            provider = ProviderRecord(
                provider="alpha",
                role=ProviderRole.EXECUTOR,
                task_id="alpha_executor_0",
                agent_config_key="alpha",
                invoker_alias="mock",
                agent_config_signature=signature_for_payload(
                    {
                        "agent_config": agent_payload,
                        "agent_config_key": "alpha",
                    }
                ),
                invoker_config_signature=signature_for_payload(invoker_payload),
            )
            first = PreflightExecutionNode(
                id="first",
                mode="sequential",
                dependencies=[],
                render_plan_id="first-render",
                provider_records=[provider],
                artifact_contract=ArtifactContract(output_path="first-result.md"),
            )
            second = PreflightExecutionNode(
                id="second",
                mode="sequential",
                dependencies=[],
                render_plan_id="second-render",
                provider_records=[
                    provider.model_copy(
                        update={"task_id": "alpha_executor_1"},
                    )
                ],
                artifact_contract=ArtifactContract(output_path="second-result.md"),
            )
            plan = PreflightExecutionPlan(
                run_id=output.run_id,
                run_key_name=output.run_key_name,
                project_root=output.base_dir.as_posix(),
                context_root=output.stages_dir.as_posix(),
                manifest_root=(output.stages_dir / "manifests").as_posix(),
                created_at="2026-06-03T00:00:00",
                workflow_name="compiled.graph",
                workflow_signature="0" * 64,
                execution_order=["first", "second"],
                nodes=[first, second],
                render_plans=[
                    RenderPlan(
                        render_plan_id="first-render",
                        streams=[
                            RenderStream(
                                target_role=ProviderRole.EXECUTOR,
                                fragments=[
                                    Fragment(
                                        fragment_index=0,
                                        kind="literal",
                                        source_role=PromptSegmentRole.SHARED,
                                        text="first",
                                    )
                                ],
                            )
                        ],
                    ),
                    RenderPlan(
                        render_plan_id="second-render",
                        streams=[
                            RenderStream(
                                target_role=ProviderRole.EXECUTOR,
                                fragments=[
                                    Fragment(
                                        fragment_index=0,
                                        kind="literal",
                                        source_role=PromptSegmentRole.SHARED,
                                        text="second",
                                    )
                                ],
                            )
                        ],
                    ),
                ],
                static_resources=[],
                token_catalog=[],
                dependency_graph=[
                    DependencyEdge(
                        source_node="first",
                        target_node="second",
                        artifact_name=None,
                        dependency_signature="first-to-second",
                    )
                ],
                runtime_config_snapshot={
                    "agents": {"alpha": agent_payload},
                    "execution": {},
                    "invoker": {**invoker_payload, "option_scopes": {}},
                    "schema_version": SCHEMA_VERSION,
                },
                effective_runtime_config_signature="1" * 64,
                fingerprint_metadata={"payload_version": "1"},
            )
            invoker = GraphDependencyOrderInvoker()

            await _execute_compiled_workflow(
                plan=plan,
                output=output,
                invoker=invoker,
                secret_context=SecretContext(),
                suppress_progress_output=True,
            )

            self.assertEqual(invoker.calls, ["first", "second"])

    async def test_parallel_node_emits_prompt_budget_warning_and_continues(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                settings=Settings(token_budget={"warn_threshold_chars": 10}),
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="alpha"),
                    "beta": AgentConfig(cli_cmd=["mock"], default_model="beta"),
                    "gamma": AgentConfig(cli_cmd=["mock"], default_model="gamma"),
                },
            )
            workflow = WorkflowPlan(
                name="dag.prompt.budget.warn",
                nodes=[
                    WorkflowNode(
                        id="node.source",
                        mode="sequential",
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED, content="source"
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                        ],
                    ),
                    WorkflowNode(
                        id="node.parallel",
                        mode="parallel",
                        needs=["node.source"],
                        prompt_segments=[
                            PromptSegment(
                                role="shared", content="Use {{node.source.output}}"
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="beta"),
                            ProviderSpec(provider="gamma"),
                        ],
                    ),
                ],
            )
            invoker = MockAgentInvoker(
                outputs=["01234567890123456789", "beta-out", "gamma-out"]
            )
            output = OutputManager(workflow.name, base_dir=tmp_path)
            events: list[ExecutionEvent] = []

            await execute_workflow(
                config,
                workflow,
                output,
                invoker=invoker,
                event_sink=events.append,
            )

            warning_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "prompt_budget_warning"
            ]
            expected_char_count = len(
                output.get_stage_output_path("node.source").read_text(encoding="utf-8")
            )
            self.assertEqual(len(warning_events), 1)
            self.assertEqual(warning_events[0].context.node_id, "node.parallel")
            self.assertIn("node.source.output", warning_events[0].payload.message)
            self.assertIn(
                "Shorten the upstream artifact", warning_events[0].payload.message
            )
            self.assertEqual(
                warning_events[0].payload.attributes,
                {
                    "upstream_node_id": "node.source",
                    "upstream_artifact_name": "output",
                    "char_count": expected_char_count,
                    "warn_threshold_chars": 10,
                },
            )
            self.assertEqual(len(invoker.calls), 3)
            self.assertIn("01234567890123456789", invoker.calls[1]["prompt"])

    async def test_downstream_prompt_uses_findings_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="alpha"),
                    "beta": AgentConfig(cli_cmd=["mock"], default_model="beta"),
                },
            )
            workflow = WorkflowPlan(
                name="dag.findings",
                nodes=[
                    WorkflowNode(
                        id="node.review",
                        mode="sequential",
                        findings=True,
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED, content="review"
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                        ],
                    ),
                    WorkflowNode(
                        id="node.summary",
                        mode="sequential",
                        needs=["node.review"],
                        prompt_segments=[
                            PromptSegment(
                                role="shared", content="Use {{node.review.findings}}"
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="beta", role=ProviderRole.EXECUTOR)
                        ],
                    ),
                ],
            )
            invoker = MockAgentInvoker(
                outputs=[
                    "\n".join(
                        [
                            "Full review output",
                            "",
                            "<!-- findings -->",
                            "concise review finding",
                            "<!-- /findings -->",
                        ]
                    ),
                    "done",
                ]
            )
            output = OutputManager(workflow.name, base_dir=tmp_path)

            await execute_workflow(config, workflow, output, invoker=invoker)

            self.assertIn("concise review finding", invoker.calls[1]["prompt"])
            self.assertTrue(output.get_stage_findings_path("node.review").exists())

    async def test_prompt_budget_warning_handles_findings_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                settings=Settings(token_budget={"warn_threshold_chars": 10}),
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="alpha"),
                    "beta": AgentConfig(cli_cmd=["mock"], default_model="beta"),
                },
            )
            workflow = WorkflowPlan(
                name="dag.findings.budget.warn",
                nodes=[
                    WorkflowNode(
                        id="node.review",
                        mode="sequential",
                        findings=True,
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED, content="review"
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                        ],
                    ),
                    WorkflowNode(
                        id="node.summary",
                        mode="sequential",
                        needs=["node.review"],
                        prompt_segments=[
                            PromptSegment(
                                role="shared", content="Use {{node.review.findings}}"
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="beta", role=ProviderRole.EXECUTOR)
                        ],
                    ),
                ],
            )
            invoker = MockAgentInvoker(
                outputs=[
                    "\n".join(
                        [
                            "Full review output",
                            "",
                            "<!-- findings -->",
                            "01234567890123456789",
                            "<!-- /findings -->",
                        ]
                    ),
                    "done",
                ]
            )
            output = OutputManager(workflow.name, base_dir=tmp_path)
            events: list[ExecutionEvent] = []

            await execute_workflow(
                config,
                workflow,
                output,
                invoker=invoker,
                event_sink=events.append,
            )

            warning_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "prompt_budget_warning"
                and event.context.node_id == "node.summary"
            ]
            self.assertEqual(len(warning_events), 1)
            self.assertIn("node.review.findings", warning_events[0].payload.message)
            self.assertEqual(
                warning_events[0].payload.attributes["upstream_artifact_name"],
                "findings",
            )

    async def test_findings_enabled_stage_fails_during_finalization_without_block(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={"alpha": AgentConfig(cli_cmd=["mock"], default_model="alpha")},
            )
            workflow = WorkflowPlan(
                name="dag.findings.invalid",
                nodes=[
                    WorkflowNode(
                        id="node.review",
                        mode="sequential",
                        findings=True,
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED, content="review"
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                        ],
                    )
                ],
            )
            invoker = MockAgentInvoker(outputs=["Full review output without findings"])
            output = OutputManager(workflow.name, base_dir=tmp_path)

            with self.assertRaisesRegex(RuntimeError, "findings block"):
                await execute_workflow(config, workflow, output, invoker=invoker)

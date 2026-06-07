import tempfile
import unittest
from pathlib import Path

from orchestrator_cli.adapters.invokers.mock import MockInvokerAdapter
from orchestrator_cli.artifacts import OutputManager
from orchestrator_cli.core.config import AgentConfig, Config, Settings
from orchestrator_cli.core.preflight import (
    DependencyEdge,
    Fragment,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    ProviderRecord,
    RenderPlan,
    RenderStream,
    signature_for_payload,
)
from orchestrator_cli.core.preflight.models import ArtifactContract
from orchestrator_cli.core.preflight.secrets import SecretContext
from orchestrator_cli.core.versions import CONFIG_SCHEMA_VERSION
from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.observability.events import ExecutionEvent
from orchestrator_cli.runtime.execution import (
    execute_workflow as _execute_compiled_workflow,
)
from orchestrator_cli.runtime.execution.consensus import (
    extract_verdict,
)
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
                version=CONFIG_SCHEMA_VERSION,
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
                            ProviderSpec(provider="exec", role="executor"),
                            ProviderSpec(provider="review", role="reviewer"),
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
                        providers=[ProviderSpec(provider="exec", role="executor")],
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
                version=CONFIG_SCHEMA_VERSION,
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
                            PromptSegment(role="shared", content="auth work")
                        ],
                        providers=[ProviderSpec(provider="alpha", role="executor")],
                    ),
                    WorkflowNode(
                        id="backend.billing",
                        mode="sequential",
                        prompt_segments=[
                            PromptSegment(role="shared", content="billing work")
                        ],
                        providers=[ProviderSpec(provider="beta", role="executor")],
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
                        providers=[ProviderSpec(provider="gamma", role="executor")],
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
                "api_version": "test",
                "capabilities": {},
                "implementation": "mock",
                "options": {},
                "resolved_identity": "mock",
            }
            provider = ProviderRecord(
                provider="alpha",
                role="executor",
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
                run_key_name=output.stages_dir.name,
                context_root=output.stages_dir.as_posix(),
                manifest_root=(output.stages_dir / "manifests").as_posix(),
                created_at="2026-06-03T00:00:00",
                workflow_name="compiled.graph",
                workflow_signature="workflow-signature",
                execution_order=["first", "second"],
                nodes=[first, second],
                render_plans=[
                    RenderPlan(
                        render_plan_id="first-render",
                        streams=[
                            RenderStream(
                                target_role="executor",
                                fragments=[
                                    Fragment(
                                        fragment_index=0,
                                        kind="literal",
                                        source_role="shared",
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
                                target_role="executor",
                                fragments=[
                                    Fragment(
                                        fragment_index=0,
                                        kind="literal",
                                        source_role="shared",
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
                },
                effective_runtime_config_signature="runtime-signature",
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
                version=CONFIG_SCHEMA_VERSION,
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
                            PromptSegment(role="shared", content="source")
                        ],
                        providers=[ProviderSpec(provider="alpha", role="executor")],
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
                and event.operation == "prompt_budget_warning"
            ]
            expected_char_count = len(
                output.get_stage_output_path("node.source").read_text(encoding="utf-8")
            )
            self.assertEqual(len(warning_events), 1)
            self.assertEqual(warning_events[0].node_id, "node.parallel")
            self.assertIn("node.source.output", warning_events[0].message)
            self.assertIn("Shorten the upstream artifact", warning_events[0].message)
            self.assertEqual(
                warning_events[0].attributes,
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
                version=CONFIG_SCHEMA_VERSION,
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
                            PromptSegment(role="shared", content="review")
                        ],
                        providers=[ProviderSpec(provider="alpha", role="executor")],
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
                        providers=[ProviderSpec(provider="beta", role="executor")],
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
                version=CONFIG_SCHEMA_VERSION,
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
                            PromptSegment(role="shared", content="review")
                        ],
                        providers=[ProviderSpec(provider="alpha", role="executor")],
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
                        providers=[ProviderSpec(provider="beta", role="executor")],
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
                and event.operation == "prompt_budget_warning"
                and event.node_id == "node.summary"
            ]
            self.assertEqual(len(warning_events), 1)
            self.assertIn("node.review.findings", warning_events[0].message)
            self.assertEqual(
                warning_events[0].attributes["upstream_artifact_name"],
                "findings",
            )

    async def test_findings_enabled_stage_fails_during_finalization_without_block(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=CONFIG_SCHEMA_VERSION,
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
                            PromptSegment(role="shared", content="review")
                        ],
                        providers=[ProviderSpec(provider="alpha", role="executor")],
                    )
                ],
            )
            invoker = MockAgentInvoker(outputs=["Full review output without findings"])
            output = OutputManager(workflow.name, base_dir=tmp_path)

            with self.assertRaisesRegex(RuntimeError, "findings block"):
                await execute_workflow(config, workflow, output, invoker=invoker)

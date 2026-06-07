import unittest

from orchestrator_cli.adapters.invokers.cli import collect_cli_availability_errors
from orchestrator_cli.core.config import AgentConfig, Config, Settings
from orchestrator_cli.core.versions import CONFIG_SCHEMA_VERSION
from orchestrator_cli.core.workflow_graph import topological_waves
from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.core.workflow_validation import (
    collect_provider_validation_errors,
    collect_token_budget_validation_errors,
    validate_token_budget_settings,
    validate_workflow_plan,
)


class WorkflowValidationProviderAndBudgetTests(unittest.TestCase):
    def test_provider_validation_reports_unknown_provider(self) -> None:
        workflow = WorkflowPlan(
            name="Workflow",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="p")],
                    providers=[ProviderSpec(provider="missing-provider")],
                )
            ],
        )
        config = Config(
            version=CONFIG_SCHEMA_VERSION,
            agents={
                "known": AgentConfig(cli_cmd=["echo"], default_model="x"),
            },
        )

        errors = collect_provider_validation_errors(workflow, config)
        self.assertEqual(len(errors), 1)
        self.assertIn("Unknown provider 'missing-provider'", errors[0])

    def test_cli_adapter_validation_reports_missing_cli(self) -> None:
        workflow = WorkflowPlan(
            name="Workflow",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="p")],
                    providers=[
                        ProviderSpec(provider="python-agent"),
                        ProviderSpec(provider="ghost-agent"),
                    ],
                )
            ],
        )
        config = Config(
            version=CONFIG_SCHEMA_VERSION,
            agents={
                "python-agent": AgentConfig(cli_cmd=["python3"], default_model="x"),
                "ghost-agent": AgentConfig(
                    cli_cmd=["definitely-not-real-executable-12345"],
                    default_model="x",
                ),
            },
        )

        errors = collect_cli_availability_errors(
            workflow,
            config,
            which_fn=lambda executable: executable if executable == "python3" else None,
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("ghost-agent", errors[0])

    def test_provider_validation_ignores_cli_availability(self) -> None:
        workflow = WorkflowPlan(
            name="Workflow",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="p")],
                    providers=[ProviderSpec(provider="ghost-agent")],
                )
            ],
        )
        config = Config(
            version=CONFIG_SCHEMA_VERSION,
            agents={
                "ghost-agent": AgentConfig(
                    cli_cmd=["definitely-not-real-executable-12345"],
                    default_model="x",
                ),
            },
        )

        errors = collect_provider_validation_errors(workflow, config)

        self.assertEqual(errors, [])

    def test_provider_validation_reports_unknown_provider_only(self) -> None:
        workflow = WorkflowPlan(
            name="Workflow",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="p")],
                    providers=[ProviderSpec(provider="missing-provider")],
                )
            ],
        )
        config = Config(version=CONFIG_SCHEMA_VERSION, agents={})

        errors = collect_provider_validation_errors(workflow, config)

        self.assertEqual(len(errors), 1)
        self.assertIn("Unknown provider 'missing-provider'", errors[0])

    def test_input_node_rejects_token_budget(self) -> None:
        workflow = WorkflowPlan(
            name="Invalid input node",
            nodes=[
                WorkflowNode(
                    id="review-input",
                    mode="input",
                    source="{{file:.orchestrator/inputs/review-findings.md}}",
                    token_budget={"warn_threshold_chars": 1000},
                )
            ],
        )
        with self.assertRaisesRegex(ValueError, "must not define token_budget"):
            validate_workflow_plan(workflow)

    def test_token_budget_validation_reports_invalid_merged_thresholds(self) -> None:
        workflow = WorkflowPlan(
            name="Workflow",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="run")],
                    token_budget={"fail_threshold_chars": 900},
                    providers=[ProviderSpec(provider="alpha", role="executor")],
                )
            ],
        )
        config = Config(
            version=CONFIG_SCHEMA_VERSION,
            agents={
                "alpha": AgentConfig(cli_cmd=["echo"], default_model="x"),
            },
            settings=Settings(
                token_budget={
                    "warn_threshold_chars": 1000,
                }
            ),
        )

        errors = collect_token_budget_validation_errors(workflow, config)
        self.assertEqual(len(errors), 1)
        self.assertIn("node.a", errors[0])
        with self.assertRaisesRegex(ValueError, "node.a"):
            validate_token_budget_settings(workflow, config)

    def test_topological_waves_preserve_frontmatter_order(self) -> None:
        workflow = WorkflowPlan(
            name="Ordered DAG",
            nodes=[
                WorkflowNode(
                    id="node.z",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="z")],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="a")],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
                WorkflowNode(
                    id="node.summary",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="sum")],
                    needs=["node.z", "node.a"],
                    providers=[ProviderSpec(provider="gpt4", role="executor")],
                ),
            ],
        )
        waves = topological_waves(workflow)
        self.assertEqual(waves[0], ["node.z", "node.a"])

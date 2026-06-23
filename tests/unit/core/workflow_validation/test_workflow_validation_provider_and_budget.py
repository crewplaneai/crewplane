import stat
import tempfile
import unittest
from pathlib import Path

from crewplane.adapters.invokers.cli import collect_cli_availability_errors
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.workflow.graph import topological_waves
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.core.workflow.validation import (
    collect_provider_validation_errors,
    collect_token_budget_validation_errors,
    validate_token_budget_settings,
    validate_workflow_plan,
)
from crewplane.version import SCHEMA_VERSION


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
            version=SCHEMA_VERSION,
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
            version=SCHEMA_VERSION,
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

    def test_cli_adapter_validation_checks_relative_path_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            executable = project_root / "tools" / "provider"
            executable.parent.mkdir()
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            workflow = WorkflowPlan(
                name="Workflow",
                nodes=[
                    WorkflowNode(
                        id="node.a",
                        mode="parallel",
                        prompt_segments=[PromptSegment(role="shared", content="p")],
                        providers=[ProviderSpec(provider="local-agent")],
                    )
                ],
            )
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "local-agent": AgentConfig(
                        cli_cmd=["tools/provider"],
                        default_model="x",
                    ),
                },
            )

            errors = collect_cli_availability_errors(
                workflow,
                config,
                which_fn=_missing_executable,
                project_root=project_root,
            )

        self.assertEqual(errors, [])

    def test_cli_adapter_validation_rejects_non_executable_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            executable = project_root / "tools" / "provider"
            executable.parent.mkdir()
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(executable.stat().st_mode & ~stat.S_IXUSR)
            workflow = WorkflowPlan(
                name="Workflow",
                nodes=[
                    WorkflowNode(
                        id="node.a",
                        mode="parallel",
                        prompt_segments=[PromptSegment(role="shared", content="p")],
                        providers=[ProviderSpec(provider="local-agent")],
                    )
                ],
            )
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "local-agent": AgentConfig(
                        cli_cmd=["tools/provider"],
                        default_model="x",
                    ),
                },
            )

            errors = collect_cli_availability_errors(
                workflow,
                config,
                which_fn=_missing_executable,
                project_root=project_root,
            )

        self.assertEqual(len(errors), 1)
        self.assertIn("local-agent", errors[0])
        self.assertIn("not found or not executable", errors[0])

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
            version=SCHEMA_VERSION,
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
        config = Config(version=SCHEMA_VERSION, agents={})

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
                    source="{{file:.crewplane/inputs/review-findings.md}}",
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
            version=SCHEMA_VERSION,
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


def _missing_executable(executable: str) -> str | None:  # noqa: ARG001
    return None

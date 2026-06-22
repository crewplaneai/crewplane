import unittest

from orchestrator_cli.core.config import AgentConfig, Config, Settings
from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.core.workflow_validation import (
    validate_audit_rounds_settings,
    validate_workflow_plan,
)
from orchestrator_cli.version import SCHEMA_VERSION


class WorkflowValidationNodeModeTests(unittest.TestCase):
    def test_parallel_node_rejects_audit_rounds(self) -> None:
        workflow = WorkflowPlan(
            name="Parallel audit rounds",
            nodes=[
                WorkflowNode(
                    id="review.parallel",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="review")],
                    audit_rounds=2,
                    providers=[ProviderSpec(provider="gpt4")],
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "does not support audit_rounds"):
            validate_workflow_plan(workflow)

    def test_workspace_exports_is_reserved_run_root_node_id(self) -> None:
        workflow = WorkflowPlan(
            name="Reserved workspace exports",
            nodes=[
                WorkflowNode(
                    id="workspace-exports",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="implement")],
                    providers=[ProviderSpec(provider="gpt4")],
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "workspace-exports.*reserved"):
            validate_workflow_plan(workflow)

    def test_input_node_rejects_audit_rounds(self) -> None:
        workflow = WorkflowPlan(
            name="Input audit rounds",
            nodes=[
                WorkflowNode(
                    id="review.input",
                    mode="input",
                    source="{{file:review.md}}",
                    audit_rounds=2,
                    providers=[],
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "must not define audit_rounds"):
            validate_workflow_plan(workflow)

    def test_single_provider_sequential_rejects_audit_rounds(self) -> None:
        workflow = WorkflowPlan(
            name="Single provider audit rounds",
            nodes=[
                WorkflowNode(
                    id="review.single",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="review")],
                    audit_rounds=2,
                    providers=[ProviderSpec(provider="gpt4", role="executor")],
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "does not support audit_rounds"):
            validate_workflow_plan(workflow)

    def test_validate_audit_rounds_settings_rejects_values_above_max(self) -> None:
        workflow = WorkflowPlan(
            name="Audit round max",
            nodes=[
                WorkflowNode(
                    id="review.loop",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="review")],
                    audit_rounds=4,
                    providers=[
                        ProviderSpec(provider="gpt4", role="executor"),
                        ProviderSpec(provider="gpt4-review", role="reviewer"),
                    ],
                )
            ],
        )
        config = Config(
            version=SCHEMA_VERSION,
            settings=Settings(max_audit_rounds=3),
            agents={
                "gpt4": AgentConfig(cli_cmd=["echo"], default_model="x"),
                "gpt4-review": AgentConfig(cli_cmd=["echo"], default_model="y"),
            },
        )

        with self.assertRaisesRegex(ValueError, "max_audit_rounds"):
            validate_audit_rounds_settings(workflow, config)

    def test_input_node_rejects_prompt_and_providers(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Invalid input",
            inputs={"review_input": "review-input"},
            nodes=[
                WorkflowNode(
                    id="review-input",
                    mode="input",
                    prompt_segments=[
                        PromptSegment(role="shared", content="unexpected")
                    ],
                    source="{{file:.orchestrator/inputs/review-findings.md}}",
                    providers=[ProviderSpec(provider="gpt4")],
                )
            ],
        )
        with self.assertRaisesRegex(ValueError, "must not define prompt_segments"):
            validate_workflow_plan(invalid_workflow)

    def test_input_node_rejects_non_bare_file_source(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Invalid input source",
            inputs={"review_input": "review-input"},
            nodes=[
                WorkflowNode(
                    id="review-input",
                    mode="input",
                    source="Review this {{file:.orchestrator/inputs/review-findings.md}}",
                )
            ],
        )
        with self.assertRaisesRegex(ValueError, "must be exactly one raw"):
            validate_workflow_plan(invalid_workflow)

    def test_input_node_rejects_findings_flag(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Invalid input findings",
            inputs={"review_input": "review-input"},
            nodes=[
                WorkflowNode(
                    id="review-input",
                    mode="input",
                    findings=True,
                    source="{{file:.orchestrator/inputs/review-findings.md}}",
                )
            ],
        )
        with self.assertRaisesRegex(ValueError, "must not define findings"):
            validate_workflow_plan(invalid_workflow)

    def test_workflow_inputs_must_reference_input_nodes(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Invalid workflow inputs",
            inputs={"review_input": "implement"},
            nodes=[
                WorkflowNode(
                    id="implement",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role="shared", content="Use raw findings")
                    ],
                    providers=[ProviderSpec(provider="gpt4", role="executor")],
                )
            ],
        )
        with self.assertRaisesRegex(ValueError, "must reference an input node"):
            validate_workflow_plan(invalid_workflow)

    def test_sequential_continue_on_failure_is_allowed(self) -> None:
        workflow = WorkflowPlan(
            name="Sequential continue allowed",
            nodes=[
                WorkflowNode(
                    id="node.seq",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role="shared", content="Review this change")
                    ],
                    continue_on_failure=True,
                    providers=[
                        ProviderSpec(provider="exec", role="executor"),
                        ProviderSpec(provider="review", role="reviewer"),
                    ],
                )
            ],
        )
        validated = validate_workflow_plan(workflow)
        self.assertEqual(validated.name, "Sequential continue allowed")

    def test_parallel_node_rejects_reviewer_roles(self) -> None:
        workflow = WorkflowPlan(
            name="Parallel reviewer invalid",
            nodes=[
                WorkflowNode(
                    id="parallel.review",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role="shared", content="Review this")
                    ],
                    providers=[ProviderSpec(provider="review", role="reviewer")],
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "does not allow reviewer roles"):
            validate_workflow_plan(workflow)

    def test_parallel_node_rejects_reviewer_prompt_segments(self) -> None:
        workflow = WorkflowPlan(
            name="Parallel reviewer prompt invalid",
            nodes=[
                WorkflowNode(
                    id="parallel.prompt.review",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role="shared", content="Base"),
                        PromptSegment(role="reviewer", content="Reviewer-only"),
                    ],
                    providers=[ProviderSpec(provider="exec", role="executor")],
                )
            ],
        )
        with self.assertRaisesRegex(ValueError, "disallowed prompt segment role"):
            validate_workflow_plan(workflow)

    def test_sequential_node_rejects_interleaved_role_segments(self) -> None:
        workflow = WorkflowPlan(
            name="Sequential interleaved roles",
            nodes=[
                WorkflowNode(
                    id="review.loop",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role="shared", content="Review this")
                    ],
                    providers=[
                        ProviderSpec(provider="exec-1", role="executor"),
                        ProviderSpec(provider="review-1", role="reviewer"),
                        ProviderSpec(provider="exec-2", role="executor"),
                    ],
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "contiguous reviewer segment"):
            validate_workflow_plan(workflow)

    def test_sequential_node_requires_terminal_reviewer(self) -> None:
        workflow = WorkflowPlan(
            name="Sequential terminal reviewer",
            nodes=[
                WorkflowNode(
                    id="review.loop",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role="shared", content="Review this")
                    ],
                    providers=[
                        ProviderSpec(provider="exec-1", role="executor"),
                        ProviderSpec(provider="exec-2", role="executor"),
                    ],
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "must end with a reviewer"):
            validate_workflow_plan(workflow)

    def test_sequential_review_loop_requires_non_empty_reviewer_prompt(self) -> None:
        workflow = WorkflowPlan(
            name="Sequential reviewer prompt required",
            nodes=[
                WorkflowNode(
                    id="review.loop.prompts",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role="executor", content="Executor only"),
                    ],
                    providers=[
                        ProviderSpec(provider="exec-1", role="executor"),
                        ProviderSpec(provider="review-1", role="reviewer"),
                    ],
                )
            ],
        )
        with self.assertRaisesRegex(
            ValueError, "rendered reviewer prompt cannot be empty"
        ):
            validate_workflow_plan(workflow)

    def test_workflow_node_mode_rejects_mixed_case_keyword(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be lower-case"):
            WorkflowNode(
                id="node.a",
                mode="Parallel",
                prompt_segments=[PromptSegment(role="shared", content="run")],
                providers=[ProviderSpec(provider="alpha")],
            )

    def test_provider_role_rejects_mixed_case_keyword(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be lower-case"):
            ProviderSpec(provider="alpha", role="Reviewer")

    def test_provider_name_rejects_blank_identifier(self) -> None:
        with self.assertRaisesRegex(ValueError, "provider"):
            ProviderSpec(provider="   ")

    def test_non_input_node_rejects_source(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Invalid source",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="run")],
                    source="{{file:.orchestrator/inputs/review-findings.md}}",
                    providers=[ProviderSpec(provider="alpha")],
                )
            ],
        )
        with self.assertRaisesRegex(ValueError, "source is only valid"):
            validate_workflow_plan(invalid_workflow)

    def test_prefixed_output_reference_must_be_upstream(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Import-like reference",
            nodes=[
                WorkflowNode(
                    id="auth.plan",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="plan")],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
                WorkflowNode(
                    id="summary.final",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role="shared", content="{{auth.plan.output}}")
                    ],
                    providers=[ProviderSpec(provider="gpt4", role="executor")],
                ),
            ],
        )
        with self.assertRaisesRegex(ValueError, "not an upstream dependency"):
            validate_workflow_plan(invalid_workflow)

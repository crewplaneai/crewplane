import unittest

from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.core.workflow_validation import (
    validate_workflow_plan,
)


class WorkflowValidationDependencyTests(unittest.TestCase):
    def test_node_id_format_validation(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Invalid ID",
            nodes=[
                WorkflowNode(
                    id="Invalid Node!",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="test")],
                    providers=[ProviderSpec(provider="gpt4")],
                )
            ],
        )
        with self.assertRaisesRegex(ValueError, "must match"):
            validate_workflow_plan(invalid_workflow)

    def test_dot_segment_node_id_rejected(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Invalid dot ID",
            nodes=[
                WorkflowNode(
                    id="..",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="test")],
                    providers=[ProviderSpec(provider="gpt4")],
                )
            ],
        )
        with self.assertRaisesRegex(ValueError, "cannot be"):
            validate_workflow_plan(invalid_workflow)

    def test_duplicate_node_ids_rejected(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Duplicate IDs",
            nodes=[
                WorkflowNode(
                    id="dup.node",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="test")],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
                WorkflowNode(
                    id="dup.node",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role="shared", content="test again")
                    ],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
            ],
        )
        with self.assertRaisesRegex(ValueError, "Duplicate node IDs"):
            validate_workflow_plan(invalid_workflow)

    def test_reserved_run_root_node_id_rejected(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Reserved ID",
            nodes=[
                WorkflowNode(
                    id="logs",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="test")],
                    providers=[ProviderSpec(provider="gpt4")],
                )
            ],
        )
        with self.assertRaisesRegex(ValueError, "reserved"):
            validate_workflow_plan(invalid_workflow)

    def test_manifests_node_id_is_also_reserved(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Reserved manifests",
            nodes=[
                WorkflowNode(
                    id="manifests",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="test")],
                    providers=[ProviderSpec(provider="gpt4")],
                )
            ],
        )
        with self.assertRaisesRegex(ValueError, "reserved"):
            validate_workflow_plan(invalid_workflow)

    def test_unknown_dependency_rejected(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Unknown dependency",
            nodes=[
                WorkflowNode(
                    id="summary.final",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="summarize")],
                    needs=["missing.node"],
                    providers=[ProviderSpec(provider="gpt4", role="executor")],
                )
            ],
        )
        with self.assertRaisesRegex(ValueError, "depends on unknown node"):
            validate_workflow_plan(invalid_workflow)

    def test_cycle_rejected(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Cycle",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="a")],
                    needs=["node.b"],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
                WorkflowNode(
                    id="node.b",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="b")],
                    needs=["node.a"],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
            ],
        )
        with self.assertRaisesRegex(ValueError, "cycle"):
            validate_workflow_plan(invalid_workflow)

    def test_output_reference_must_be_upstream(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Bad reference",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="A")],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
                WorkflowNode(
                    id="node.b",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role="shared", content="{{node.a.output}}")
                    ],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
            ],
        )
        with self.assertRaisesRegex(ValueError, "not an upstream dependency"):
            validate_workflow_plan(invalid_workflow)

    def test_output_reference_unknown_node_rejected(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Unknown output",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role="shared", content="A {{missing.output}}")
                    ],
                    providers=[ProviderSpec(provider="gpt4")],
                )
            ],
        )
        with self.assertRaisesRegex(ValueError, "references unknown output"):
            validate_workflow_plan(invalid_workflow)

    def test_valid_upstream_output_reference(self) -> None:
        workflow = WorkflowPlan(
            name="Valid reference",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="A")],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
                WorkflowNode(
                    id="node.b",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="B")],
                    needs=["node.a"],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
                WorkflowNode(
                    id="summary.final",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(
                            role="shared",
                            content="{{node.a.output}}\n{{node.b.output}}",
                        )
                    ],
                    needs=["node.b"],
                    providers=[ProviderSpec(provider="gpt4", role="executor")],
                ),
            ],
        )
        validated = validate_workflow_plan(workflow)
        self.assertEqual(validated.name, "Valid reference")

    def test_findings_reference_requires_upstream_findings_flag(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Missing findings flag",
            nodes=[
                WorkflowNode(
                    id="node.review",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="Review")],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
                WorkflowNode(
                    id="node.summary",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role="shared", content="{{node.review.findings}}")
                    ],
                    needs=["node.review"],
                    providers=[ProviderSpec(provider="gpt4", role="executor")],
                ),
            ],
        )
        with self.assertRaisesRegex(ValueError, "does not define findings: true"):
            validate_workflow_plan(invalid_workflow)

    def test_valid_upstream_findings_reference(self) -> None:
        workflow = WorkflowPlan(
            name="Valid findings reference",
            nodes=[
                WorkflowNode(
                    id="node.review",
                    mode="parallel",
                    findings=True,
                    prompt_segments=[PromptSegment(role="shared", content="Review")],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
                WorkflowNode(
                    id="node.summary",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role="shared", content="{{node.review.findings}}")
                    ],
                    needs=["node.review"],
                    providers=[ProviderSpec(provider="gpt4", role="executor")],
                ),
            ],
        )
        validated = validate_workflow_plan(workflow)
        self.assertEqual(validated.name, "Valid findings reference")

    def test_valid_upstream_output_path_reference(self) -> None:
        workflow = WorkflowPlan(
            name="Valid output path reference",
            nodes=[
                WorkflowNode(
                    id="node.build",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="Build")],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
                WorkflowNode(
                    id="node.summary",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(
                            role="shared", content="{{node.build.output_path}}"
                        )
                    ],
                    needs=["node.build"],
                    providers=[ProviderSpec(provider="gpt4", role="executor")],
                ),
            ],
        )

        validated = validate_workflow_plan(workflow)
        self.assertEqual(validated.name, "Valid output path reference")

    def test_findings_path_reference_requires_upstream_findings_flag(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Missing findings path flag",
            nodes=[
                WorkflowNode(
                    id="node.review",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="Review")],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
                WorkflowNode(
                    id="node.summary",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(
                            role="shared",
                            content="{{node.review.findings_path}}",
                        )
                    ],
                    needs=["node.review"],
                    providers=[ProviderSpec(provider="gpt4", role="executor")],
                ),
            ],
        )

        with self.assertRaisesRegex(ValueError, "does not define findings: true"):
            validate_workflow_plan(invalid_workflow)

    def test_unknown_artifact_reference_rejected(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Unknown artifact",
            nodes=[
                WorkflowNode(
                    id="node.review",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="Review")],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
                WorkflowNode(
                    id="node.summary",
                    mode="sequential",
                    needs=["node.review"],
                    prompt_segments=[
                        PromptSegment(role="shared", content="{{node.review.summary}}")
                    ],
                    providers=[ProviderSpec(provider="gpt4", role="executor")],
                ),
            ],
        )
        with self.assertRaisesRegex(ValueError, "unsupported artifact"):
            validate_workflow_plan(invalid_workflow)

    def test_mixed_case_artifact_reference_rejected(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Mixed case artifact",
            nodes=[
                WorkflowNode(
                    id="node.review",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="Review")],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
                WorkflowNode(
                    id="node.summary",
                    mode="sequential",
                    needs=["node.review"],
                    prompt_segments=[
                        PromptSegment(role="shared", content="{{node.review.Output}}")
                    ],
                    providers=[ProviderSpec(provider="gpt4", role="executor")],
                ),
            ],
        )
        with self.assertRaisesRegex(ValueError, "lower-case"):
            validate_workflow_plan(invalid_workflow)

    def test_malformed_balanced_template_token_rejected(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Malformed template token",
            nodes=[
                WorkflowNode(
                    id="node.runtime",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(
                            role="shared",
                            content="Review {{src/orchestrator_cli/runtime}}",
                        )
                    ],
                    providers=[ProviderSpec(provider="gpt4")],
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "unsupported template"):
            validate_workflow_plan(invalid_workflow)

    def test_empty_balanced_template_token_rejected(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Empty template token",
            nodes=[
                WorkflowNode(
                    id="node.runtime",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role="shared", content="Review {{}}")
                    ],
                    providers=[ProviderSpec(provider="gpt4")],
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "unsupported template"):
            validate_workflow_plan(invalid_workflow)

    def test_supported_non_artifact_template_tokens_allowed(self) -> None:
        workflow = WorkflowPlan(
            name="Supported template tokens",
            nodes=[
                WorkflowNode(
                    id="node.templates",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(
                            role="shared",
                            content=(
                                "Use {{env:HOME}} {{file:README.md}} "
                                "{{var:project_name}}"
                            ),
                        )
                    ],
                    providers=[ProviderSpec(provider="gpt4")],
                )
            ],
        )

        validated = validate_workflow_plan(workflow)
        self.assertEqual(validated.name, "Supported template tokens")

    def test_param_template_is_rejected_after_composition_boundary(self) -> None:
        workflow = WorkflowPlan(
            name="Composition-only param",
            nodes=[
                WorkflowNode(
                    id="node.templates",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(
                            role="shared",
                            content="Use {{param:module_name}}",
                        )
                    ],
                    providers=[ProviderSpec(provider="gpt4")],
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "composition-only"):
            validate_workflow_plan(workflow)

    def test_mixed_case_keyword_template_rejected(self) -> None:
        invalid_workflow = WorkflowPlan(
            name="Mixed case keyword template",
            nodes=[
                WorkflowNode(
                    id="node.templates",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role="shared", content="Use {{Var:project_name}}")
                    ],
                    providers=[ProviderSpec(provider="gpt4")],
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "lower-case"):
            validate_workflow_plan(invalid_workflow)

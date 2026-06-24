import unittest

from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.preflight.validation import (
    collect_preflight_workflow_reference_diagnostics,
    validate_preflight_workflow_references,
)
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.core.workflow.validation import (
    collect_workflow_validation_diagnostics,
    validate_audit_rounds_settings,
    validate_workflow_plan,
)
from crewplane.version import SCHEMA_VERSION


class WorkflowValidationNodeModeTests(unittest.TestCase):
    def test_parallel_node_rejects_audit_rounds(self) -> None:
        workflow = WorkflowPlan(
            name="Parallel audit rounds",
            nodes=[
                WorkflowNode(
                    id="review.parallel",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="review")
                    ],
                    audit_rounds=2,
                    providers=[ProviderSpec(provider="gpt4")],
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "does not support audit_rounds"):
            validate_workflow_plan(workflow)

    def test_parallel_node_rejects_explicit_review_starts_with(self) -> None:
        workflow = WorkflowPlan(
            name="Parallel review starts with",
            nodes=[
                WorkflowNode(
                    id="review.parallel",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="review")
                    ],
                    review_starts_with="executor",
                    providers=[ProviderSpec(provider="gpt4")],
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "does not support review_starts_with"):
            validate_workflow_plan(workflow)

    def test_workspace_exports_is_reserved_run_root_node_id(self) -> None:
        workflow = WorkflowPlan(
            name="Reserved workspace exports",
            nodes=[
                WorkflowNode(
                    id="workspace-exports",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(
                            role=PromptSegmentRole.SHARED, content="implement"
                        )
                    ],
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

    def test_input_node_rejects_explicit_review_starts_with(self) -> None:
        workflow = WorkflowPlan(
            name="Input review starts with",
            nodes=[
                WorkflowNode(
                    id="review.input",
                    mode="input",
                    source="{{file:review.md}}",
                    review_starts_with="executor",
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "must not define review_starts_with"):
            validate_workflow_plan(workflow)

    def test_single_provider_sequential_rejects_audit_rounds(self) -> None:
        workflow = WorkflowPlan(
            name="Single provider audit rounds",
            nodes=[
                WorkflowNode(
                    id="review.single",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="review")
                    ],
                    audit_rounds=2,
                    providers=[
                        ProviderSpec(provider="gpt4", role=ProviderRole.EXECUTOR)
                    ],
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "does not support audit_rounds"):
            validate_workflow_plan(workflow)

    def test_single_provider_sequential_rejects_explicit_review_starts_with(
        self,
    ) -> None:
        workflow = WorkflowPlan(
            name="Single provider review starts with",
            nodes=[
                WorkflowNode(
                    id="review.single",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="review")
                    ],
                    review_starts_with="executor",
                    providers=[
                        ProviderSpec(provider="gpt4", role=ProviderRole.EXECUTOR)
                    ],
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "does not support review_starts_with"):
            validate_workflow_plan(workflow)

    def test_validate_audit_rounds_settings_rejects_values_above_max(self) -> None:
        workflow = WorkflowPlan(
            name="Audit round max",
            nodes=[
                WorkflowNode(
                    id="review.loop",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="review")
                    ],
                    audit_rounds=4,
                    providers=[
                        ProviderSpec(provider="gpt4", role=ProviderRole.EXECUTOR),
                        ProviderSpec(
                            provider="gpt4-review", role=ProviderRole.REVIEWER
                        ),
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
                        PromptSegment(
                            role=PromptSegmentRole.SHARED, content="unexpected"
                        )
                    ],
                    source="{{file:.crewplane/inputs/review-findings.md}}",
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
                    source="Review this {{file:.crewplane/inputs/review-findings.md}}",
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
                    source="{{file:.crewplane/inputs/review-findings.md}}",
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
                        PromptSegment(
                            role=PromptSegmentRole.SHARED, content="Use raw findings"
                        )
                    ],
                    providers=[
                        ProviderSpec(provider="gpt4", role=ProviderRole.EXECUTOR)
                    ],
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
                        PromptSegment(
                            role=PromptSegmentRole.SHARED, content="Review this change"
                        )
                    ],
                    continue_on_failure=True,
                    providers=[
                        ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                        ProviderSpec(provider="review", role=ProviderRole.REVIEWER),
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
                        PromptSegment(
                            role=PromptSegmentRole.SHARED, content="Review this"
                        )
                    ],
                    providers=[
                        ProviderSpec(provider="review", role=ProviderRole.REVIEWER)
                    ],
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
                        PromptSegment(role=PromptSegmentRole.SHARED, content="Base"),
                        PromptSegment(
                            role=PromptSegmentRole.REVIEWER, content="Reviewer-only"
                        ),
                    ],
                    providers=[
                        ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR)
                    ],
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
                        PromptSegment(
                            role=PromptSegmentRole.SHARED, content="Review this"
                        )
                    ],
                    providers=[
                        ProviderSpec(provider="exec-1", role=ProviderRole.EXECUTOR),
                        ProviderSpec(provider="review-1", role=ProviderRole.REVIEWER),
                        ProviderSpec(provider="exec-2", role=ProviderRole.EXECUTOR),
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
                        PromptSegment(
                            role=PromptSegmentRole.SHARED, content="Review this"
                        )
                    ],
                    providers=[
                        ProviderSpec(provider="exec-1", role=ProviderRole.EXECUTOR),
                        ProviderSpec(provider="exec-2", role=ProviderRole.EXECUTOR),
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
                        PromptSegment(
                            role=PromptSegmentRole.EXECUTOR, content="Executor only"
                        ),
                    ],
                    providers=[
                        ProviderSpec(provider="exec-1", role=ProviderRole.EXECUTOR),
                        ProviderSpec(provider="review-1", role=ProviderRole.REVIEWER),
                    ],
                )
            ],
        )
        with self.assertRaisesRegex(
            ValueError, "rendered reviewer prompt cannot be empty"
        ):
            validate_workflow_plan(workflow)

    def test_standalone_reviewer_first_without_static_context_warns_only(
        self,
    ) -> None:
        workflow = WorkflowPlan(
            name="Standalone reviewer first",
            nodes=[
                self._review_loop_node(
                    review_starts_with="reviewer",
                    prompt_segments=[
                        PromptSegment(
                            role=PromptSegmentRole.SHARED,
                            content="Review the current project state.",
                        )
                    ],
                )
            ],
        )

        diagnostics = collect_workflow_validation_diagnostics(workflow)
        warnings = [
            diagnostic for diagnostic in diagnostics if diagnostic.severity == "warning"
        ]
        preflight_diagnostics = collect_preflight_workflow_reference_diagnostics(
            workflow
        )

        self.assertEqual(len(warnings), 1)
        self.assertIn("starts with a reviewer", warnings[0].message)
        self.assertIs(validate_workflow_plan(workflow), workflow)
        self.assertIs(validate_preflight_workflow_references(workflow), workflow)
        self.assertEqual(
            [diagnostic.severity for diagnostic in preflight_diagnostics],
            ["warning"],
        )

    def test_reviewer_first_missing_context_warning_ignores_executor_segments(
        self,
    ) -> None:
        workflow = WorkflowPlan(
            name="Executor-only context",
            nodes=[
                self._review_loop_node(
                    review_starts_with="reviewer",
                    prompt_segments=[
                        PromptSegment(
                            role=PromptSegmentRole.EXECUTOR,
                            content="Use {{file:README.md}}.",
                        ),
                        PromptSegment(
                            role=PromptSegmentRole.REVIEWER,
                            content="Review the current state.",
                        ),
                    ],
                )
            ],
        )

        self.assertEqual(len(self._warning_messages(workflow)), 1)

    def test_reviewer_first_static_context_suppresses_missing_context_warning(
        self,
    ) -> None:
        for role in (PromptSegmentRole.SHARED, PromptSegmentRole.REVIEWER):
            with self.subTest(role=role):
                workflow = WorkflowPlan(
                    name="Static review context",
                    nodes=[
                        self._review_loop_node(
                            review_starts_with="reviewer",
                            prompt_segments=[
                                PromptSegment(
                                    role=PromptSegmentRole.SHARED,
                                    content="Execute the final handoff.",
                                ),
                                PromptSegment(
                                    role=role,
                                    content="Review {{file:README.md}}.",
                                ),
                            ],
                        )
                    ],
                )

                self.assertEqual(self._warning_messages(workflow), [])

    def test_reviewer_first_needs_suppress_missing_context_warning(self) -> None:
        workflow = WorkflowPlan(
            name="Upstream review context",
            nodes=[
                WorkflowNode(
                    id="context",
                    mode="sequential",
                    providers=[ProviderSpec(provider="gpt4")],
                    prompt_segments=[
                        PromptSegment(
                            role=PromptSegmentRole.SHARED,
                            content="Build context.",
                        )
                    ],
                ),
                self._review_loop_node(
                    review_starts_with="reviewer",
                    needs=["context"],
                    prompt_segments=[
                        PromptSegment(
                            role=PromptSegmentRole.SHARED,
                            content=(
                                "Review {{context.output}}, {{context.findings}}, "
                                "and {{context.output_sha256}}."
                            ),
                        )
                    ],
                ),
            ],
        )

        self.assertEqual(self._warning_messages(workflow), [])

    def test_reviewer_first_artifact_reference_suppresses_warning_even_if_invalid(
        self,
    ) -> None:
        workflow = WorkflowPlan(
            name="Artifact review context",
            nodes=[
                self._review_loop_node(
                    review_starts_with="reviewer",
                    prompt_segments=[
                        PromptSegment(
                            role=PromptSegmentRole.SHARED,
                            content="Review {{context.findings_size}}.",
                        )
                    ],
                )
            ],
        )

        self.assertEqual(self._warning_messages(workflow), [])

    def test_workflow_node_mode_rejects_mixed_case_keyword(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be lower-case"):
            WorkflowNode(
                id="node.a",
                mode="Parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                ],
                providers=[ProviderSpec(provider="alpha")],
            )

    def test_review_starts_with_rejects_unknown_keyword(self) -> None:
        with self.assertRaisesRegex(ValueError, "review_starts_with must be one of"):
            WorkflowNode(
                id="node.a",
                mode="sequential",
                review_starts_with="planner",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                ],
                providers=[ProviderSpec(provider="alpha")],
            )

    def test_review_starts_with_rejects_mixed_case_keyword(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "review_starts_with must be lower-case"
        ):
            WorkflowNode(
                id="node.a",
                mode="sequential",
                review_starts_with="Reviewer",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                ],
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
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                    ],
                    source="{{file:.crewplane/inputs/review-findings.md}}",
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
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="plan")
                    ],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
                WorkflowNode(
                    id="summary.final",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(
                            role=PromptSegmentRole.SHARED,
                            content="{{auth.plan.output}}",
                        )
                    ],
                    providers=[
                        ProviderSpec(provider="gpt4", role=ProviderRole.EXECUTOR)
                    ],
                ),
            ],
        )
        with self.assertRaisesRegex(ValueError, "not an upstream dependency"):
            validate_workflow_plan(invalid_workflow)

    def _review_loop_node(
        self,
        review_starts_with: str,
        prompt_segments: list[PromptSegment],
        needs: list[str] | None = None,
    ) -> WorkflowNode:
        return WorkflowNode(
            id="review.loop",
            mode="sequential",
            review_starts_with=review_starts_with,
            needs=needs or [],
            providers=[
                ProviderSpec(provider="codex", role=ProviderRole.EXECUTOR),
                ProviderSpec(provider="claude", role=ProviderRole.REVIEWER),
            ],
            prompt_segments=prompt_segments,
        )

    def _warning_messages(self, workflow: WorkflowPlan) -> list[str]:
        return [
            diagnostic.message
            for diagnostic in collect_workflow_validation_diagnostics(workflow)
            if diagnostic.severity == "warning"
        ]

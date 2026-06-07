import tempfile
import unittest
from pathlib import Path

from orchestrator_cli.core.versions import WORKFLOW_SCHEMA_VERSION
from orchestrator_cli.core.workflow_loader import load_tasks
from orchestrator_cli.core.workflow_models import WorkflowNode, render_prompt_for_role
from orchestrator_cli.core.workflow_validation import validate_workflow_plan


def _executor_prompt(node: WorkflowNode) -> str:
    return render_prompt_for_role(node, "executor")


class WorkflowMarkdownLoadingTests(unittest.TestCase):
    def test_load_workflow_markdown(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                "name: Full Feature Workflow",
                "description: End-to-end",
                "nodes:",
                "  - id: backend.auth",
                "    mode: parallel",
                "    providers: [claude, gemini]",
                "  - id: summary.final",
                "    mode: sequential",
                "    needs: [backend.auth]",
                "    providers: [claude]",
                "---",
                "",
                "## backend.auth",
                "",
                "Analyze {{file:spec.md}}.",
                "",
                "## summary.final",
                "",
                "Summarize {{backend.auth.output}}.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            workflow = validate_workflow_plan(load_tasks(path))

        self.assertEqual(workflow.schema_version, WORKFLOW_SCHEMA_VERSION)
        self.assertEqual(workflow.name, "Full Feature Workflow")
        self.assertEqual(len(workflow.nodes), 2)
        self.assertEqual(
            _executor_prompt(workflow.nodes[0]).strip(),
            "Analyze {{file:spec.md}}.",
        )
        self.assertEqual(
            _executor_prompt(workflow.nodes[1]).strip(),
            "Summarize {{backend.auth.output}}.",
        )

    def test_workflow_markdown_allows_input_node_without_body_section(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                "name: Workflow",
                "inputs:",
                "  review_input: review-input",
                "nodes:",
                "  - id: review-input",
                "    mode: input",
                '    source: "{{file:.orchestrator/inputs/review-findings.md}}"',
                "  - id: implement",
                "    mode: sequential",
                "    needs: [review-input]",
                "    providers: [claude]",
                "---",
                "",
                "## implement",
                "",
                "Use {{review-input.output}}.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            workflow = validate_workflow_plan(load_tasks(path))

        self.assertEqual(workflow.inputs, {"review_input": "review-input"})
        self.assertEqual(workflow.nodes[0].mode, "input")
        self.assertEqual(
            workflow.nodes[0].source,
            "{{file:.orchestrator/inputs/review-findings.md}}",
        )
        self.assertEqual(workflow.nodes[0].prompt_segments, [])

    def test_workflow_markdown_rejects_input_node_section_even_when_empty(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                "name: Workflow",
                "inputs:",
                "  review_input: review-input",
                "nodes:",
                "  - id: review-input",
                "    mode: input",
                '    source: "{{file:.orchestrator/inputs/review-findings.md}}"',
                "  - id: implement",
                "    mode: sequential",
                "    needs: [review-input]",
                "    providers: [claude]",
                "---",
                "",
                "## review-input",
                "",
                "## review-input",
                "",
                "## implement",
                "",
                "Use {{review-input.output}}.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, "must not define a markdown section"
            ):
                load_tasks(path)

    def test_workflow_markdown_rejects_source_on_non_input_node(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                "name: Workflow",
                "nodes:",
                "  - id: implement",
                "    mode: sequential",
                '    source: "{{file:.orchestrator/inputs/review-findings.md}}"',
                "    providers: [claude]",
                "---",
                "",
                "## implement",
                "",
                "Use the source.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source is only valid"):
                load_tasks(path)

    def test_workflow_markdown_loads_node_token_budget(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                "name: Workflow",
                "nodes:",
                "  - id: implement",
                "    mode: sequential",
                "    token_budget:",
                "      warn_threshold_chars: 1200",
                "      fail_threshold_chars: 2400",
                "    providers: [claude]",
                "---",
                "",
                "## implement",
                "",
                "Use {{upstream.output}}.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            workflow = load_tasks(path)

        self.assertIsNotNone(workflow.nodes[0].token_budget)
        assert workflow.nodes[0].token_budget is not None
        self.assertEqual(workflow.nodes[0].token_budget.warn_threshold_chars, 1200)
        self.assertEqual(workflow.nodes[0].token_budget.fail_threshold_chars, 2400)

    def test_workflow_markdown_parses_role_scoped_prompt_segments(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                "name: Workflow",
                "nodes:",
                "  - id: review.iterate",
                "    mode: sequential",
                "    providers:",
                "      - provider: codex",
                "        role: executor",
                "      - provider: claude",
                "        role: reviewer",
                "---",
                "",
                "## review.iterate",
                "",
                "Shared context.",
                "",
                "<!-- orchestrator:executor -->",
                "Executor instructions.",
                "<!-- /orchestrator:executor -->",
                "",
                "<!-- orchestrator:reviewer -->",
                "Reviewer instructions.",
                "<!-- /orchestrator:reviewer -->",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            workflow = validate_workflow_plan(load_tasks(path))

        node = workflow.nodes[0]
        roles = [segment.role for segment in node.prompt_segments]
        self.assertEqual(roles, ["shared", "executor", "shared", "reviewer"])
        self.assertIn("Shared context.", _executor_prompt(node))
        self.assertIn("Executor instructions.", _executor_prompt(node))
        self.assertIn(
            "Reviewer instructions.", render_prompt_for_role(node, "reviewer")
        )

    def test_workflow_markdown_rejects_nested_role_markers(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                "name: Workflow",
                "nodes:",
                "  - id: review.iterate",
                "    mode: sequential",
                "    providers:",
                "      - provider: codex",
                "        role: executor",
                "      - provider: claude",
                "        role: reviewer",
                "---",
                "",
                "## review.iterate",
                "",
                "<!-- orchestrator:executor -->",
                "Outer block.",
                "<!-- orchestrator:reviewer -->",
                "Nested block.",
                "<!-- /orchestrator:reviewer -->",
                "<!-- /orchestrator:executor -->",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "nested orchestrator role markers"):
                load_tasks(path)

    def test_workflow_markdown_treats_marker_text_in_code_block_as_literal(
        self,
    ) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                "name: Workflow",
                "nodes:",
                "  - id: review.node",
                "    mode: sequential",
                "    providers: [codex]",
                "---",
                "",
                "## review.node",
                "",
                "```md",
                "<!-- orchestrator:reviewer -->",
                "```",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            workflow = validate_workflow_plan(load_tasks(path))

        self.assertIn(
            "<!-- orchestrator:reviewer -->", _executor_prompt(workflow.nodes[0])
        )

    def test_workflow_markdown_treats_nested_markdown_marker_text_as_literal(
        self,
    ) -> None:
        cases = {
            "blockquote": [
                "> <!-- orchestrator:reviewer -->",
                "> Reviewer marker text.",
                "> <!-- /orchestrator:reviewer -->",
            ],
            "list": [
                "- <!-- orchestrator:reviewer -->",
                "  Reviewer marker text.",
                "  <!-- /orchestrator:reviewer -->",
            ],
        }
        for case_name, body_lines in cases.items():
            workflow_content = "\n".join(
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Workflow",
                    "nodes:",
                    "  - id: review.node",
                    "    mode: sequential",
                    "    providers: [codex]",
                    "---",
                    "",
                    "## review.node",
                    "",
                    *body_lines,
                ]
            )
            with self.subTest(case_name=case_name):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    path = Path(tmp_dir) / "workflow.task.md"
                    path.write_text(workflow_content, encoding="utf-8")
                    workflow = validate_workflow_plan(load_tasks(path))

                self.assertIn(
                    "<!-- orchestrator:reviewer -->",
                    _executor_prompt(workflow.nodes[0]),
                )

    def test_workflow_markdown_allows_non_marker_orchestrator_comment(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                "name: Workflow",
                "nodes:",
                "  - id: review.node",
                "    mode: sequential",
                "    providers: [codex]",
                "---",
                "",
                "## review.node",
                "",
                "<!-- orchestrator runtime note -->",
                "Keep this literal.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            workflow = validate_workflow_plan(load_tasks(path))

        rendered = _executor_prompt(workflow.nodes[0])
        self.assertIn("<!-- orchestrator runtime note -->", rendered)
        self.assertIn("Keep this literal.", rendered)

    def test_workflow_markdown_preserves_crlf_and_trailing_spaces(self) -> None:
        workflow_content = (
            "---\r\n"
            f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"\r\n'
            "name: Workflow\r\n"
            "nodes:\r\n"
            "  - id: review.node\r\n"
            "    mode: sequential\r\n"
            "    providers: [codex]\r\n"
            "---\r\n"
            "\r\n"
            "## review.node\r\n"
            "\r\n"
            "Line one with spaces.   \r\n"
            "Line two.\r\n"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            workflow = validate_workflow_plan(load_tasks(path))

        rendered = _executor_prompt(workflow.nodes[0])
        self.assertIn("Line one with spaces.   \r\n", rendered)
        self.assertIn("Line two.\r\n", rendered)

    def test_workflow_markdown_loads_node_findings_flag(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                "name: Workflow",
                "nodes:",
                "  - id: review",
                "    mode: sequential",
                "    findings: true",
                "    providers: [claude]",
                "  - id: implement",
                "    mode: sequential",
                "    needs: [review]",
                "    providers: [codex]",
                "---",
                "",
                "## review",
                "",
                "Review and emit findings.",
                "",
                "## implement",
                "",
                "Use {{review.findings}}.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            workflow = validate_workflow_plan(load_tasks(path))

        self.assertTrue(workflow.nodes[0].findings)
        self.assertEqual(
            _executor_prompt(workflow.nodes[1]).strip(), "Use {{review.findings}}."
        )

    def test_workflow_markdown_loads_and_serializes_audit_rounds(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                "name: Workflow",
                "nodes:",
                "  - id: review.loop",
                "    mode: sequential",
                "    depth: 2",
                "    audit_rounds: 3",
                "    providers:",
                "      - provider: codex",
                "        role: executor",
                "      - provider: claude",
                "        role: reviewer",
                "---",
                "",
                "## review.loop",
                "",
                "Review this implementation.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            workflow = load_tasks(path)

        self.assertEqual(workflow.nodes[0].audit_rounds, 3)
        self.assertEqual(
            workflow.nodes[0].model_dump(exclude_none=True)["audit_rounds"],
            3,
        )

    def test_workflow_markdown_requires_node_section(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                "name: Workflow",
                "nodes:",
                "  - id: backend.auth",
                "    mode: parallel",
                "    providers: [claude]",
                "---",
                "",
                "## another.node",
                "",
                "Prompt text.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Missing node section"):
                load_tasks(path)

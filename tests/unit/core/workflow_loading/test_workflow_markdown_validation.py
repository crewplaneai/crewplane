import tempfile
import unittest
from pathlib import Path

from crewplane.core.workflow.loading import load_tasks, load_tasks_with_sources
from crewplane.core.workflow.markdown import validate_workflow_markdown
from crewplane.core.workflow.models import WorkflowNode, render_prompt_for_role
from crewplane.core.workflow.validation import validate_workflow_plan
from crewplane.version import SCHEMA_VERSION


def _executor_prompt(node: WorkflowNode) -> str:
    return render_prompt_for_role(node, "executor")


class WorkflowMarkdownValidationTests(unittest.TestCase):
    def test_workflow_markdown_rejects_input_node_body_section(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Workflow",
                "inputs:",
                "  review_input: review-input",
                "nodes:",
                "  - id: review-input",
                "    mode: input",
                '    source: "{{file:.crewplane/inputs/review-findings.md}}"',
                "  - id: implement",
                "    mode: sequential",
                "    needs: [review-input]",
                "    providers: [claude]",
                "---",
                "",
                "## review-input",
                "",
                "Do not allow this.",
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

    def test_workflow_markdown_treats_unrecognized_headings_as_literal(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Workflow",
                "nodes:",
                "  - id: backend.auth",
                "    mode: parallel",
                "    providers: [claude]",
                "---",
                "",
                "## backend.auth",
                "",
                "Prompt text.",
                "",
                "## orphan.node",
                "",
                "Unexpected prompt.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            workflow = validate_workflow_plan(load_tasks(path))

        rendered = _executor_prompt(workflow.nodes[0])
        self.assertIn("Prompt text.", rendered)
        self.assertIn("## orphan.node", rendered)
        self.assertIn("Unexpected prompt.", rendered)

    def test_workflow_markdown_rejects_duplicate_node_section(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Workflow",
                "nodes:",
                "  - id: backend.auth",
                "    mode: parallel",
                "    providers: [claude]",
                "---",
                "",
                "## backend.auth",
                "",
                "Prompt text.",
                "",
                "## backend.auth",
                "",
                "Duplicate prompt.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate node section"):
                load_tasks(path)

    def test_workflow_markdown_rejects_unknown_provider_keys(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Workflow",
                "nodes:",
                "  - id: review.node",
                "    mode: sequential",
                "    providers:",
                "      - provider: claude",
                "        rol: reviewer",
                "---",
                "",
                "## review.node",
                "",
                "Review this.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Extra inputs are not permitted"):
                load_tasks(path)

    def test_validate_workflow_markdown_rejects_duplicate_provider_keys(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Workflow",
                "nodes:",
                "  - id: review.node",
                "    mode: sequential",
                "    providers:",
                "      - provider: codex",
                "        role: gpt-5.3-codex-spark",
                "        role: executor",
                "---",
                "",
                "## review.node",
                "",
                "Review this.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate YAML key 'role'"):
                validate_workflow_markdown(path)

    def test_load_tasks_rejects_duplicate_provider_keys_in_import(self) -> None:
        root_content = "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Root Workflow",
                "imports:",
                "  - path: imported.task.md",
                "    as: imported",
                "nodes:",
                "  - id: summary.final",
                "    mode: sequential",
                "    needs: [imported.review.node]",
                "    providers: [codex]",
                "---",
                "",
                "## summary.final",
                "",
                "Summarize {{imported.review.node.output}}.",
            ]
        )
        imported_content = "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Imported Workflow",
                "nodes:",
                "  - id: review.node",
                "    mode: sequential",
                "    providers:",
                "      - provider: codex",
                "        role: gpt-5.3-codex-spark",
                "        role: executor",
                "---",
                "",
                "## review.node",
                "",
                "Review this.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            root_path = tmp_path / "root.task.md"
            imported_path = tmp_path / "imported.task.md"
            root_path.write_text(root_content, encoding="utf-8")
            imported_path.write_text(imported_content, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "duplicate YAML key 'role'"):
                load_tasks_with_sources(root_path, project_root=tmp_path)

    def test_load_tasks_rejects_duplicate_keys_in_legacy_yaml(self) -> None:
        workflow_content = "\n".join(
            [
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Workflow",
                "nodes:",
                "  - id: backend.auth",
                "    mode: parallel",
                "    mode: sequential",
                "    providers:",
                "      - provider: claude",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.yaml"
            path.write_text(workflow_content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate YAML key 'mode'"):
                load_tasks(path)

    def test_workflow_markdown_rejects_duplicate_inputs_after_normalization(
        self,
    ) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Workflow",
                "inputs:",
                "  review_input: review-input",
                "  ' review_input ': alternate-input",
                "nodes:",
                "  - id: review-input",
                "    mode: input",
                '    source: "{{file:.crewplane/inputs/review.md}}"',
                "  - id: alternate-input",
                "    mode: input",
                '    source: "{{file:.crewplane/inputs/alternate.md}}"',
                "---",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate workflow input key"):
                load_tasks(path)

    def test_workflow_markdown_rejects_duplicate_import_inputs_after_normalization(
        self,
    ) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Workflow",
                "imports:",
                "  - path: child.task.md",
                "    as: child",
                "    inputs:",
                "      review_input: review-input",
                "      ' review_input ': alternate-input",
                "nodes:",
                "  - id: summary.final",
                "    mode: sequential",
                "    providers: [codex]",
                "---",
                "",
                "## summary.final",
                "",
                "Summarize.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "Duplicate workflow import input key",
            ):
                load_tasks(path)

    def test_workflow_markdown_rejects_mixed_case_mode_keyword(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Workflow",
                "nodes:",
                "  - id: review.node",
                "    mode: Sequential",
                "    providers: [claude]",
                "---",
                "",
                "## review.node",
                "",
                "Review this.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be lower-case"):
                load_tasks(path)

    def test_workflow_markdown_rejects_mixed_case_role_keyword(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Workflow",
                "nodes:",
                "  - id: review.node",
                "    mode: sequential",
                "    providers:",
                "      - provider: claude",
                "        role: Reviewer",
                "---",
                "",
                "## review.node",
                "",
                "Review this.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be lower-case"):
                load_tasks(path)

    def test_workflow_markdown_keeps_non_node_h2_headings_inside_section(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Workflow",
                "nodes:",
                "  - id: backend.auth",
                "    mode: parallel",
                "    providers: [claude]",
                "---",
                "",
                "## backend.auth",
                "",
                "Intro text.",
                "",
                "## Background",
                "",
                "More details.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            workflow = validate_workflow_plan(load_tasks(path))

        rendered = _executor_prompt(workflow.nodes[0])
        self.assertIn("Intro text.", rendered)
        self.assertIn("## Background", rendered)
        self.assertIn("More details.", rendered)

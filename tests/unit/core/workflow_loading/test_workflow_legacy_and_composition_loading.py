import tempfile
import unittest
from pathlib import Path

from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.loading import load_tasks, load_tasks_with_sources
from crewplane.core.workflow.markdown import validate_workflow_markdown
from crewplane.core.workflow.models import WorkflowNode, render_prompt_for_role
from crewplane.core.workflow.validation import validate_workflow_plan
from crewplane.version import SCHEMA_VERSION


def _executor_prompt(node: WorkflowNode) -> str:
    return render_prompt_for_role(node, PromptSegmentRole.EXECUTOR)


class WorkflowLegacyAndCompositionLoadingTests(unittest.TestCase):
    def test_load_tasks_rejects_non_object_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.yaml"
            path.write_text('"just-a-string"', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "YAML object"):
                load_tasks(path)

    def test_load_tasks_rejects_unsupported_schema_version(self) -> None:
        workflow_content = "\n".join(
            [
                "---",
                'schema_version: "99.0"',
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
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.task.md"
            path.write_text(workflow_content, encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                f"Expected '{SCHEMA_VERSION}'",
            ):
                load_tasks(path)

    def test_load_tasks_rejects_yaml_imports(self) -> None:
        workflow_content = "\n".join(
            [
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Workflow",
                "imports:",
                "  - path: child.task.md",
                "    as: child",
                "nodes:",
                "  - id: backend.auth",
                "    mode: sequential",
                "    prompt: Prompt text.",
                "    providers:",
                "      - provider: claude",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.yaml"
            path.write_text(workflow_content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "imports"):
                load_tasks(path)

    def test_yaml_workflow_param_template_is_rejected_before_runtime(self) -> None:
        workflow_content = "\n".join(
            [
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Workflow",
                "nodes:",
                "  - id: backend.auth",
                "    mode: sequential",
                "    prompt_segments:",
                "      - role: shared",
                '        content: "Build {{param:module_name}}."',
                "    providers:",
                "      - provider: claude",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "workflow.yaml"
            path.write_text(workflow_content, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "composition-only"):
                validate_workflow_plan(load_tasks(path))

    def test_validate_workflow_markdown_rejects_provider_mapping_typo(self) -> None:
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
            with self.assertRaisesRegex(ValueError, "rol"):
                validate_workflow_markdown(path)

    def test_load_tasks_with_sources_composes_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            module_path = tmp_path / "module.task.md"
            workflow_path = tmp_path / "root.task.md"

            module_path.write_text(
                "\n".join(
                    [
                        "---",
                        f'schema_version: "{SCHEMA_VERSION}"',
                        "name: Auth Module",
                        "nodes:",
                        "  - id: plan",
                        "    mode: parallel",
                        "    providers: [claude]",
                        "---",
                        "",
                        "## plan",
                        "",
                        "Build {{param:module_name}}.",
                    ]
                ),
                encoding="utf-8",
            )
            workflow_path.write_text(
                "\n".join(
                    [
                        "---",
                        f'schema_version: "{SCHEMA_VERSION}"',
                        "name: Root Workflow",
                        "imports:",
                        "  - path: module.task.md",
                        "    as: auth",
                        "    with:",
                        "      module_name: payments-auth",
                        "nodes:",
                        "  - id: summary.final",
                        "    mode: sequential",
                        "    needs: [auth.plan]",
                        "    providers: [claude]",
                        "---",
                        "",
                        "## summary.final",
                        "",
                        "Summarize {{auth.plan.output}}.",
                    ]
                ),
                encoding="utf-8",
            )

            load_result = load_tasks_with_sources(workflow_path, project_root=tmp_path)
            workflow = validate_workflow_plan(load_result.workflow)

        self.assertEqual(
            [node.id for node in workflow.nodes], ["auth.plan", "summary.final"]
        )
        self.assertIn("payments-auth", _executor_prompt(workflow.nodes[0]))

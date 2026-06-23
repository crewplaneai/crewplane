import tempfile
import unittest
from pathlib import Path

from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.loading import load_tasks_with_sources
from crewplane.core.workflow.models import WorkflowNode, render_prompt_for_role
from crewplane.version import SCHEMA_VERSION


def _write_workflow(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines), encoding="utf-8")


def _executor_prompt(node: WorkflowNode) -> str:
    return render_prompt_for_role(node, PromptSegmentRole.EXECUTOR)


class WorkflowCompositionRejectionTests(unittest.TestCase):
    def test_duplicate_import_alias_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module_a = root / "module-a.task.md"
            module_b = root / "module-b.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                module_a,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Module A",
                    "nodes:",
                    "  - id: plan",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## plan",
                    "",
                    "A",
                ],
            )
            _write_workflow(
                module_b,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Module B",
                    "nodes:",
                    "  - id: plan",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## plan",
                    "",
                    "B",
                ],
            )
            _write_workflow(
                workflow,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: module-a.task.md",
                    "    as: auth",
                    "  - path: module-b.task.md",
                    "    as: auth",
                    "nodes:",
                    "  - id: summary",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## summary",
                    "",
                    "Summary",
                ],
            )

            with self.assertRaisesRegex(ValueError, "duplicate import alias"):
                load_tasks_with_sources(workflow, project_root=root)

    def test_import_outside_project_root_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "project"
            root.mkdir(parents=True)
            external = Path(tmp_dir) / "external.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                external,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: External",
                    "nodes:",
                    "  - id: plan",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## plan",
                    "",
                    "External",
                ],
            )
            _write_workflow(
                workflow,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    f"  - path: {external}",
                    "    as: ext",
                    "nodes:",
                    "  - id: summary",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## summary",
                    "",
                    "Summary",
                ],
            )

            with self.assertRaisesRegex(ValueError, "outside project root"):
                load_tasks_with_sources(workflow, project_root=root)

    def test_file_env_var_templates_are_not_rewritten_by_composition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                module,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Module",
                    "nodes:",
                    "  - id: plan",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## plan",
                    "",
                    "File={{file:spec.md}} Env={{env:BRANCH_NAME}} Var={{var:project_name}}",
                ],
            )
            _write_workflow(
                workflow,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: module.task.md",
                    "    as: auth",
                    "nodes:",
                    "  - id: summary",
                    "    mode: sequential",
                    "    needs: [auth.plan]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## summary",
                    "",
                    "Summary {{auth.plan.output}}",
                ],
            )

            workflow_plan = load_tasks_with_sources(
                workflow,
                project_root=root,
            ).workflow

        prompt = _executor_prompt(workflow_plan.nodes[0])
        self.assertIn("{{file:spec.md}}", prompt)
        self.assertIn("{{env:BRANCH_NAME}}", prompt)
        self.assertIn("{{var:project_name}}", prompt)

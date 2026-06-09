import tempfile
import unittest
from pathlib import Path

from orchestrator_cli.core.workflow_loader import load_tasks_with_sources
from orchestrator_cli.core.workflow_models import WorkflowNode, render_prompt_for_role
from orchestrator_cli.core.workflow_validation import validate_workflow_plan
from orchestrator_cli.versions import WORKFLOW_SCHEMA_VERSION


def _write_workflow(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines), encoding="utf-8")


def _executor_prompt(node: WorkflowNode) -> str:
    return render_prompt_for_role(node, "executor")


class WorkflowCompositionImportTests(unittest.TestCase):
    def test_composes_imported_workflow_and_rewrites_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                module,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Auth Module",
                    "nodes:",
                    "  - id: plan",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## plan",
                    "",
                    "Build {{param:module_name}} for {{var:project_name}}.",
                ],
            )
            _write_workflow(
                workflow,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: module.task.md",
                    "    as: auth",
                    "    with:",
                    "      module_name: payments-auth",
                    "nodes:",
                    "  - id: summary.final",
                    "    mode: sequential",
                    "    needs: [auth.plan]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## summary.final",
                    "",
                    "Summarize {{auth.plan.output}}.",
                ],
            )

            load_result = load_tasks_with_sources(workflow, project_root=root)
            validated = validate_workflow_plan(load_result.workflow)

        self.assertEqual(
            [node.id for node in validated.nodes],
            ["auth.plan", "summary.final"],
        )
        self.assertIn("payments-auth", _executor_prompt(validated.nodes[0]))
        self.assertIn("{{var:project_name}}", _executor_prompt(validated.nodes[0]))
        self.assertEqual(
            [record.path.name for record in load_result.referenced_workflows],
            ["root.task.md", "module.task.md"],
        )

    def test_composes_imported_workflow_and_preserves_findings_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                module,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Review Module",
                    "nodes:",
                    "  - id: review",
                    "    mode: sequential",
                    "    findings: true",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## review",
                    "",
                    "Review the module.",
                ],
            )
            _write_workflow(
                workflow,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: module.task.md",
                    "    as: quality",
                    "nodes:",
                    "  - id: implement",
                    "    mode: sequential",
                    "    needs: [quality.review]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## implement",
                    "",
                    "Use {{quality.review.findings}}.",
                ],
            )

            validated = validate_workflow_plan(
                load_tasks_with_sources(workflow, project_root=root).workflow
            )

        self.assertEqual(
            [node.id for node in validated.nodes],
            ["quality.review", "implement"],
        )
        self.assertIn(
            "{{quality.review.findings}}", _executor_prompt(validated.nodes[1])
        )

    def test_unbound_param_template_rewrites_to_var_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                module,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Module",
                    "nodes:",
                    "  - id: plan",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## plan",
                    "",
                    "Use {{param:module_name}}.",
                ],
            )
            _write_workflow(
                workflow,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
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
                    "done",
                ],
            )

            workflow_plan = load_tasks_with_sources(
                workflow,
                project_root=root,
            ).workflow

        self.assertIn("{{var:module_name}}", _executor_prompt(workflow_plan.nodes[0]))

    def test_import_with_rejects_unused_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                module,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Module",
                    "nodes:",
                    "  - id: plan",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## plan",
                    "",
                    "No params here.",
                ],
            )
            _write_workflow(
                workflow,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: module.task.md",
                    "    as: auth",
                    "    with:",
                    "      module_name: payments-auth",
                    "nodes:",
                    "  - id: summary",
                    "    mode: sequential",
                    "    needs: [auth.plan]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## summary",
                    "",
                    "done",
                ],
            )

            with self.assertRaisesRegex(ValueError, "unused parameter"):
                load_tasks_with_sources(workflow, project_root=root)

    def test_import_with_rejects_shadowed_unused_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            leaf = root / "leaf.task.md"
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                leaf,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Leaf",
                    "nodes:",
                    "  - id: plan",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## plan",
                    "",
                    "Leaf {{param:module_name}}",
                ],
            )
            _write_workflow(
                module,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Module",
                    "imports:",
                    "  - path: leaf.task.md",
                    "    as: leaf",
                    "    with:",
                    "      module_name: inner",
                    "nodes:",
                    "  - id: finalize",
                    "    mode: sequential",
                    "    needs: [leaf.plan]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## finalize",
                    "",
                    "Finalize {{leaf.plan.output}}",
                ],
            )
            _write_workflow(
                workflow,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: module.task.md",
                    "    as: auth",
                    "    with:",
                    "      module_name: outer",
                    "nodes:",
                    "  - id: summary",
                    "    mode: sequential",
                    "    needs: [auth.finalize]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## summary",
                    "",
                    "Summary",
                ],
            )

            with self.assertRaisesRegex(ValueError, "unused parameter"):
                load_tasks_with_sources(workflow, project_root=root)

    def test_import_cycle_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workflow_a = root / "a.task.md"
            workflow_b = root / "b.task.md"

            _write_workflow(
                workflow_a,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Workflow A",
                    "imports:",
                    "  - path: b.task.md",
                    "    as: b",
                    "nodes:",
                    "  - id: a.node",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## a.node",
                    "",
                    "A",
                ],
            )
            _write_workflow(
                workflow_b,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Workflow B",
                    "imports:",
                    "  - path: a.task.md",
                    "    as: a",
                    "nodes:",
                    "  - id: b.node",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## b.node",
                    "",
                    "B",
                ],
            )

            with self.assertRaisesRegex(ValueError, "cycle"):
                load_tasks_with_sources(workflow_a, project_root=root)

    def test_composition_rejects_node_id_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                module,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Module",
                    "nodes:",
                    "  - id: plan",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## plan",
                    "",
                    "Plan",
                ],
            )
            _write_workflow(
                workflow,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: module.task.md",
                    "    as: auth",
                    "nodes:",
                    "  - id: auth.plan",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## auth.plan",
                    "",
                    "Conflict",
                ],
            )

            with self.assertRaisesRegex(ValueError, "Node ID collision"):
                load_tasks_with_sources(workflow, project_root=root)

    def test_imported_schema_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                module,
                [
                    "---",
                    'schema_version: "99.0"',
                    "name: Module",
                    "nodes:",
                    "  - id: plan",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## plan",
                    "",
                    "Plan",
                ],
            )
            _write_workflow(
                workflow,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: module.task.md",
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

            with self.assertRaisesRegex(
                ValueError, "Unsupported workflow schema version"
            ):
                load_tasks_with_sources(workflow, project_root=root)

    def test_nested_imports_compose_alias_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared.task.md"
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                shared,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Shared",
                    "nodes:",
                    "  - id: normalize",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## normalize",
                    "",
                    "Normalize",
                ],
            )
            _write_workflow(
                module,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Module",
                    "imports:",
                    "  - path: shared.task.md",
                    "    as: shared",
                    "nodes:",
                    "  - id: finalize",
                    "    mode: sequential",
                    "    needs: [shared.normalize]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## finalize",
                    "",
                    "Use {{shared.normalize.output}}",
                ],
            )
            _write_workflow(
                workflow,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: module.task.md",
                    "    as: auth",
                    "nodes:",
                    "  - id: summary",
                    "    mode: sequential",
                    "    needs: [auth.finalize]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## summary",
                    "",
                    "Summary",
                ],
            )

            workflow_plan = load_tasks_with_sources(
                workflow,
                project_root=root,
            ).workflow

        ids = [node.id for node in workflow_plan.nodes]
        self.assertEqual(ids, ["auth.shared.normalize", "auth.finalize", "summary"])
        self.assertEqual(workflow_plan.nodes[1].needs, ["auth.shared.normalize"])
        self.assertIn(
            "{{auth.shared.normalize.output}}",
            _executor_prompt(workflow_plan.nodes[1]),
        )

    def test_output_reference_requires_upstream_dependency_across_import_boundary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                module,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Module",
                    "nodes:",
                    "  - id: plan",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## plan",
                    "",
                    "Plan",
                ],
            )
            _write_workflow(
                workflow,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: module.task.md",
                    "    as: auth",
                    "nodes:",
                    "  - id: summary",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## summary",
                    "",
                    "Use {{auth.plan.output}}",
                ],
            )

            workflow_plan = load_tasks_with_sources(
                workflow,
                project_root=root,
            ).workflow
            with self.assertRaisesRegex(ValueError, "not an upstream dependency"):
                validate_workflow_plan(workflow_plan)

    def test_root_node_can_feed_imported_namespace_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            review = root / "review.task.md"
            fixer = root / "fix.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                review,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Review",
                    "nodes:",
                    "  - id: findings",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## findings",
                    "",
                    "Findings",
                ],
            )
            _write_workflow(
                fixer,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Fix",
                    "nodes:",
                    "  - id: implement",
                    "    mode: sequential",
                    "    needs: [review-input]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## implement",
                    "",
                    "Use {{review-input.output}}",
                ],
            )
            _write_workflow(
                workflow,
                [
                    "---",
                    f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: review.task.md",
                    "    as: quality",
                    "  - path: fix.task.md",
                    "    as: fix",
                    "nodes:",
                    "  - id: fix.review-input",
                    "    mode: sequential",
                    "    needs: [quality.findings]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## fix.review-input",
                    "",
                    "Pass through {{quality.findings.output}}",
                ],
            )

            validated = validate_workflow_plan(
                load_tasks_with_sources(workflow, project_root=root).workflow
            )

        self.assertEqual(
            [node.id for node in validated.nodes],
            ["quality.findings", "fix.implement", "fix.review-input"],
        )
        self.assertEqual(validated.nodes[1].needs, ["fix.review-input"])
        self.assertIn(
            "{{fix.review-input.output}}", _executor_prompt(validated.nodes[1])
        )

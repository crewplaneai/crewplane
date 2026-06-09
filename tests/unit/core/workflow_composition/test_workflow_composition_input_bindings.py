import tempfile
import unittest
from pathlib import Path

from orchestrator_cli.core.workflow_loader import load_tasks_with_sources
from orchestrator_cli.core.workflow_models import WorkflowNode, render_prompt_for_role
from orchestrator_cli.core.workflow_validation import validate_workflow_plan
from orchestrator_cli.version import SCHEMA_VERSION


def _write_workflow(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines), encoding="utf-8")


def _executor_prompt(node: WorkflowNode) -> str:
    return render_prompt_for_role(node, "executor")


class WorkflowCompositionInputBindingTests(unittest.TestCase):
    def test_import_input_binding_rewrites_dependencies_and_preserves_unbound_inputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            producer = root / "producer.task.md"
            consumer = root / "consumer.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                producer,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Producer",
                    "nodes:",
                    "  - id: review.findings",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## review.findings",
                    "",
                    "Findings",
                ],
            )
            _write_workflow(
                consumer,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Consumer",
                    "inputs:",
                    "  review_input: review-input",
                    "  standards_input: standards-input",
                    "nodes:",
                    "  - id: review-input",
                    "    mode: input",
                    '    source: "{{file:.orchestrator/inputs/review-findings.md}}"',
                    "  - id: standards-input",
                    "    mode: input",
                    '    source: "{{file:.orchestrator/inputs/coding-standards.md}}"',
                    "  - id: implement",
                    "    mode: sequential",
                    "    needs: [review-input, standards-input]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## implement",
                    "",
                    "Use findings: {{review-input.output}}",
                    "",
                    "Use standards: {{standards-input.output}}",
                ],
            )
            _write_workflow(
                workflow,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: producer.task.md",
                    "    as: quality",
                    "  - path: consumer.task.md",
                    "    as: fix",
                    "    inputs:",
                    "      review_input: quality.review.findings",
                    "nodes:",
                    "  - id: handoff",
                    "    mode: sequential",
                    "    needs: [fix.implement]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## handoff",
                    "",
                    "Done {{fix.implement.output}}",
                ],
            )

            validated = validate_workflow_plan(
                load_tasks_with_sources(workflow, project_root=root).workflow
            )

        self.assertEqual(
            [node.id for node in validated.nodes],
            [
                "quality.review.findings",
                "fix.standards-input",
                "fix.implement",
                "handoff",
            ],
        )
        self.assertEqual(validated.nodes[1].mode, "input")
        self.assertEqual(
            validated.nodes[2].needs,
            ["quality.review.findings", "fix.standards-input"],
        )
        self.assertIn(
            "{{quality.review.findings.output}}",
            _executor_prompt(validated.nodes[2]),
        )
        self.assertIn(
            "{{fix.standards-input.output}}",
            _executor_prompt(validated.nodes[2]),
        )

    def test_bound_import_input_ignores_invalid_fallback_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            producer = root / "producer.task.md"
            consumer = root / "consumer.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                producer,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Producer",
                    "nodes:",
                    "  - id: review.findings",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## review.findings",
                    "",
                    "Findings",
                ],
            )
            _write_workflow(
                consumer,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Consumer",
                    "inputs:",
                    "  review_input: review-input",
                    "nodes:",
                    "  - id: review-input",
                    "    mode: input",
                    '    source: "not a raw file template"',
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
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: producer.task.md",
                    "    as: quality",
                    "  - path: consumer.task.md",
                    "    as: fix",
                    "    inputs:",
                    "      review_input: quality.review.findings",
                    "nodes:",
                    "  - id: handoff",
                    "    mode: sequential",
                    "    needs: [fix.implement]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## handoff",
                    "",
                    "Done {{fix.implement.output}}",
                ],
            )

            validated = validate_workflow_plan(
                load_tasks_with_sources(workflow, project_root=root).workflow
            )

        self.assertEqual(
            [node.id for node in validated.nodes],
            ["quality.review.findings", "fix.implement", "handoff"],
        )
        self.assertEqual(validated.nodes[1].needs, ["quality.review.findings"])
        self.assertIn(
            "{{quality.review.findings.output}}",
            _executor_prompt(validated.nodes[1]),
        )

    def test_bound_import_input_ignores_missing_fallback_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            producer = root / "producer.task.md"
            consumer = root / "consumer.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                producer,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Producer",
                    "nodes:",
                    "  - id: review.findings",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## review.findings",
                    "",
                    "Findings",
                ],
            )
            _write_workflow(
                consumer,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Consumer",
                    "inputs:",
                    "  review_input: review-input",
                    "nodes:",
                    "  - id: review-input",
                    "    mode: input",
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
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: producer.task.md",
                    "    as: quality",
                    "  - path: consumer.task.md",
                    "    as: fix",
                    "    inputs:",
                    "      review_input: quality.review.findings",
                    "nodes:",
                    "  - id: handoff",
                    "    mode: sequential",
                    "    needs: [fix.implement]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## handoff",
                    "",
                    "Done {{fix.implement.output}}",
                ],
            )

            validated = validate_workflow_plan(
                load_tasks_with_sources(workflow, project_root=root).workflow
            )

        self.assertEqual(
            [node.id for node in validated.nodes],
            ["quality.review.findings", "fix.implement", "handoff"],
        )
        self.assertEqual(validated.nodes[1].needs, ["quality.review.findings"])

    def test_unbound_import_input_rejects_invalid_fallback_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            consumer = root / "consumer.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                consumer,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Consumer",
                    "inputs:",
                    "  review_input: review-input",
                    "nodes:",
                    "  - id: review-input",
                    "    mode: input",
                    '    source: "not a raw file template"',
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
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: consumer.task.md",
                    "    as: fix",
                    "nodes:",
                    "  - id: handoff",
                    "    mode: sequential",
                    "    needs: [fix.implement]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## handoff",
                    "",
                    "Done {{fix.implement.output}}",
                ],
            )

            loaded = load_tasks_with_sources(workflow, project_root=root).workflow

        with self.assertRaisesRegex(ValueError, "must be exactly one raw"):
            validate_workflow_plan(loaded)

    def test_standalone_workflow_rejects_invalid_input_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workflow = root / "consumer.task.md"

            _write_workflow(
                workflow,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Consumer",
                    "inputs:",
                    "  review_input: review-input",
                    "nodes:",
                    "  - id: review-input",
                    "    mode: input",
                    '    source: "not a raw file template"',
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

            loaded = load_tasks_with_sources(workflow, project_root=root).workflow

        with self.assertRaisesRegex(ValueError, "must be exactly one raw"):
            validate_workflow_plan(loaded)

    def test_missing_import_file_rejected_even_with_input_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workflow = root / "root.task.md"

            _write_workflow(
                workflow,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: missing-consumer.task.md",
                    "    as: fix",
                    "    inputs:",
                    "      review_input: local.findings",
                    "nodes:",
                    "  - id: local.findings",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## local.findings",
                    "",
                    "Local findings",
                ],
            )

            with self.assertRaisesRegex(ValueError, "Imported workflow does not exist"):
                load_tasks_with_sources(workflow, project_root=root)

    def test_import_input_binding_rejects_unknown_input_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                module,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Consumer",
                    "inputs:",
                    "  review_input: review-input",
                    "nodes:",
                    "  - id: review-input",
                    "    mode: input",
                    '    source: "{{file:.orchestrator/inputs/review-findings.md}}"',
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
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: module.task.md",
                    "    as: fix",
                    "    inputs:",
                    "      missing_input: local.findings",
                    "nodes:",
                    "  - id: local.findings",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## local.findings",
                    "",
                    "Local findings",
                ],
            )

            with self.assertRaisesRegex(ValueError, "does not declare input"):
                load_tasks_with_sources(workflow, project_root=root)

    def test_import_input_binding_can_target_local_root_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                module,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Consumer",
                    "inputs:",
                    "  review_input: review-input",
                    "nodes:",
                    "  - id: review-input",
                    "    mode: input",
                    '    source: "{{file:.orchestrator/inputs/review-findings.md}}"',
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
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: module.task.md",
                    "    as: fix",
                    "    inputs:",
                    "      review_input: local.findings",
                    "nodes:",
                    "  - id: local.findings",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## local.findings",
                    "",
                    "Local findings",
                ],
            )

            validated = validate_workflow_plan(
                load_tasks_with_sources(workflow, project_root=root).workflow
            )

        self.assertEqual(
            [node.id for node in validated.nodes],
            ["fix.implement", "local.findings"],
        )
        self.assertEqual(validated.nodes[0].needs, ["local.findings"])
        self.assertIn("{{local.findings.output}}", _executor_prompt(validated.nodes[0]))

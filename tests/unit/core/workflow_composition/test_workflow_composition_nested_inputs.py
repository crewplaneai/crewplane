import unittest
from pathlib import Path
from tempfile import mkdtemp

import pytest
import yaml

from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.loading import load_tasks_with_sources
from crewplane.core.workflow.models import WorkflowNode, render_prompt_for_role
from crewplane.core.workflow.validation import validate_workflow_plan
from crewplane.version import SCHEMA_VERSION


def _write_workflow(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines), encoding="utf-8")


def _executor_prompt(node: WorkflowNode) -> str:
    return render_prompt_for_role(node, PromptSegmentRole.EXECUTOR)


@pytest.mark.parametrize("same_source", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_nested_aliases_compare_canonical_sources(tmp_path, same_source, reverse):
    def write(name, header, body):
        path = tmp_path / f"{name}.task.md"
        path.write_text(
            "---\n"
            + yaml.safe_dump(
                {"schema_version": SCHEMA_VERSION, "name": name, **header},
                sort_keys=False,
            )
            + "---\n"
            + body,
            encoding="utf-8",
            newline="\n",
        )
        return path

    def fallback(node_id):
        return {"id": node_id, "mode": "input", "source": "{{file:unused.md}}"}

    write(
        "child",
        {
            "inputs": {"left": "fallback", "right": "fallback"},
            "nodes": [
                fallback("fallback"),
                {
                    "id": "use",
                    "mode": "sequential",
                    "providers": ["alpha"],
                    "needs": ["fallback"],
                },
            ],
        },
        "## use\nUse {{fallback.output}}\n",
    )
    bindings = [("left", "first"), ("right", "second")]
    write(
        "module",
        {
            "inputs": {"first_input": "first", "second_input": "second"},
            "imports": [
                {
                    "path": "child.task.md",
                    "as": "inner",
                    "inputs": dict(reversed(bindings) if reverse else bindings),
                }
            ],
            "nodes": [fallback("first"), fallback("second")],
        },
        "",
    )
    path = write(
        "root",
        {
            "imports": [
                {
                    "path": "module.task.md",
                    "as": "outer",
                    "inputs": {
                        "first_input": "source.one",
                        "second_input": "source.one" if same_source else "source.two",
                    },
                }
            ],
            "nodes": [
                {"id": node_id, "mode": "sequential", "providers": ["alpha"]}
                for node_id in ("source.one", "source.two")
            ],
        },
        "## source.one\nOne\n## source.two\nTwo\n",
    )
    if not same_source:
        with pytest.raises(ValueError) as raised:
            load_tasks_with_sources(path, project_root=tmp_path)
        for expected in (
            "child.task.md",
            "fallback",
            "left",
            "right",
            "source.one",
            "source.two",
        ):
            assert expected in str(raised.value)
    else:
        workflow = load_tasks_with_sources(path, project_root=tmp_path).workflow
        node = next(node for node in workflow.nodes if node.id == "outer.inner.use")
        assert node.needs == ["source.one"]
        assert "{{source.one.output}}" in _executor_prompt(node)
        assert not any(node.mode == "input" for node in workflow.nodes)


class WorkflowCompositionNestedInputTests(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def temporary_directory_root(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    def test_nested_import_input_binding_propagates_through_alias_chain(self) -> None:
        tmp_dir = mkdtemp(dir=self.tmp_path)
        root = Path(tmp_dir)
        producer = root / "producer.task.md"
        consumer = root / "consumer.task.md"
        module = root / "module.task.md"
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
                '    source: "{{file:.crewplane/inputs/review-findings.md}}"',
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
            module,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Module",
                "inputs:",
                "  review_input: review-input",
                "imports:",
                "  - path: consumer.task.md",
                "    as: fix",
                "    inputs:",
                "      review_input: review-input",
                "nodes:",
                "  - id: review-input",
                "    mode: input",
                '    source: "{{file:.crewplane/inputs/review-findings.md}}"',
                "  - id: handoff",
                "    mode: sequential",
                "    needs: [fix.implement]",
                "    providers: [alpha]",
                "---",
                "",
                "## handoff",
                "",
                "Handoff {{fix.implement.output}}",
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
                "  - path: module.task.md",
                "    as: bundle",
                "    inputs:",
                "      review_input: quality.review.findings",
                "nodes:",
                "  - id: summary",
                "    mode: sequential",
                "    needs: [bundle.handoff]",
                "    providers: [alpha]",
                "---",
                "",
                "## summary",
                "",
                "Summary {{bundle.handoff.output}}",
            ],
        )

        validated = validate_workflow_plan(
            load_tasks_with_sources(workflow, project_root=root).workflow
        )

        self.assertEqual(
            [node.id for node in validated.nodes],
            [
                "quality.review.findings",
                "bundle.fix.implement",
                "bundle.handoff",
                "summary",
            ],
        )
        self.assertEqual(validated.nodes[1].needs, ["quality.review.findings"])
        self.assertIn(
            "{{quality.review.findings.output}}",
            _executor_prompt(validated.nodes[1]),
        )

    def test_imported_workflow_input_declaration_must_reference_input_node(
        self,
    ) -> None:
        tmp_dir = mkdtemp(dir=self.tmp_path)
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
                "  review_input: implement",
                "nodes:",
                "  - id: implement",
                "    mode: sequential",
                "    providers: [alpha]",
                "---",
                "",
                "## implement",
                "",
                "Use findings",
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

        with self.assertRaisesRegex(ValueError, "must reference an input node"):
            load_tasks_with_sources(workflow, project_root=root)

    def test_bound_import_input_rejects_non_root_input_node(self) -> None:
        tmp_dir = mkdtemp(dir=self.tmp_path)
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
                '    source: "{{file:.crewplane/inputs/review-findings.md}}"',
                "    needs: [prepare]",
                "  - id: prepare",
                "    mode: sequential",
                "    providers: [alpha]",
                "  - id: implement",
                "    mode: sequential",
                "    needs: [review-input]",
                "    providers: [alpha]",
                "---",
                "",
                "## prepare",
                "",
                "Prepare review input",
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
                "      review_input: upstream.findings",
                "nodes:",
                "  - id: upstream.findings",
                "    mode: sequential",
                "    providers: [alpha]",
                "---",
                "",
                "## upstream.findings",
                "",
                "Upstream findings",
            ],
        )

        with self.assertRaisesRegex(ValueError, "must not define dependencies"):
            load_tasks_with_sources(workflow, project_root=root)

    def test_bound_import_input_rejects_findings_node(self) -> None:
        tmp_dir = mkdtemp(dir=self.tmp_path)
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
                "    findings: true",
                '    source: "{{file:.crewplane/inputs/review-findings.md}}"',
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
                "      review_input: upstream.findings",
                "nodes:",
                "  - id: upstream.findings",
                "    mode: sequential",
                "    providers: [alpha]",
                "---",
                "",
                "## upstream.findings",
                "",
                "Upstream findings",
            ],
        )

        with self.assertRaisesRegex(ValueError, "must not define findings"):
            load_tasks_with_sources(workflow, project_root=root)

    def test_bound_import_input_rejects_token_budget(self) -> None:
        tmp_dir = mkdtemp(dir=self.tmp_path)
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
                '    source: "{{file:.crewplane/inputs/review-findings.md}}"',
                "    token_budget:",
                "      warn_threshold_chars: 100",
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
                "      review_input: upstream.findings",
                "nodes:",
                "  - id: upstream.findings",
                "    mode: sequential",
                "    providers: [alpha]",
                "---",
                "",
                "## upstream.findings",
                "",
                "Upstream findings",
            ],
        )

        with self.assertRaisesRegex(ValueError, "must not define token_budget"):
            load_tasks_with_sources(workflow, project_root=root)

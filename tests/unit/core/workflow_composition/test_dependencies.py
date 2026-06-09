from pathlib import Path

from orchestrator_cli.core.workflow_loader import load_tasks_with_sources
from orchestrator_cli.core.workflow_models import WorkflowNode, render_prompt_for_role
from orchestrator_cli.core.workflow_validation import validate_workflow_plan
from orchestrator_cli.version import SCHEMA_VERSION


def _write_workflow(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines), encoding="utf-8")


def _executor_prompt(node: WorkflowNode) -> str:
    return render_prompt_for_role(node, "executor")


def test_import_input_binding_rewrites_dependency_to_canonical_caller_locator(
    tmp_path: Path,
) -> None:
    producer = tmp_path / "producer.task.md"
    consumer = tmp_path / "consumer.task.md"
    workflow = tmp_path / "root.task.md"
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
            '    source: "{{file:.orchestrator/inputs/review-findings.md}}"',
            "  - id: implement",
            "    mode: sequential",
            "    needs: [review-input]",
            "    providers: [alpha]",
            "---",
            "",
            "## implement",
            "",
            "Use findings: {{review-input.output}}",
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
            "nodes: []",
            "---",
        ],
    )

    loaded = load_tasks_with_sources(workflow, project_root=tmp_path).workflow
    validated = validate_workflow_plan(loaded)

    assert [node.id for node in validated.nodes] == [
        "quality.review.findings",
        "fix.implement",
    ]
    implement = validated.nodes[1]
    assert implement.needs == ["quality.review.findings"]
    assert "{{quality.review.findings.output}}" in _executor_prompt(implement)

from __future__ import annotations

from pathlib import Path

import pytest

from crewplane.core.workflow.loading import load_tasks_with_sources
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.core.workflow.validation import validate_workflow_plan
from crewplane.version import SCHEMA_VERSION


def write_workflow(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines), encoding="utf-8")


def load_validated_workflow(path: Path, project_root: Path) -> WorkflowPlan:
    return validate_workflow_plan(
        load_tasks_with_sources(path, project_root=project_root).workflow
    )


def build_namespaced_import_workflow(tmp_path: Path) -> WorkflowPlan:
    module_path = tmp_path / "module.task.md"
    workflow_path = tmp_path / "root.task.md"

    write_workflow(
        module_path,
        [
            "---",
            f'schema_version: "{SCHEMA_VERSION}"',
            "name: Auth Module",
            "nodes:",
            "  - id: plan",
            "    mode: sequential",
            "    providers: [alpha]",
            "---",
            "",
            "## plan",
            "",
            "Build auth module.",
        ],
    )
    write_workflow(
        workflow_path,
        [
            "---",
            f'schema_version: "{SCHEMA_VERSION}"',
            "name: Root",
            "imports:",
            "  - path: module.task.md",
            "    as: auth",
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
    return load_validated_workflow(workflow_path, tmp_path)


def build_imported_input_workflow(tmp_path: Path) -> WorkflowPlan:
    inputs_dir = tmp_path / ".crewplane" / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    (inputs_dir / "review-findings.md").write_text("Review findings", encoding="utf-8")

    consumer_path = tmp_path / "consumer.task.md"
    workflow_path = tmp_path / "root.task.md"

    write_workflow(
        consumer_path,
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
    write_workflow(
        workflow_path,
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
            "Handoff {{fix.implement.output}}",
        ],
    )
    return load_validated_workflow(workflow_path, tmp_path)


def build_bound_input_rewrite_workflow(tmp_path: Path) -> WorkflowPlan:
    inputs_dir = tmp_path / ".crewplane" / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    (inputs_dir / "coding-standards.md").write_text(
        "Coding standards", encoding="utf-8"
    )

    producer_path = tmp_path / "producer.task.md"
    consumer_path = tmp_path / "consumer.task.md"
    workflow_path = tmp_path / "root.task.md"

    write_workflow(
        producer_path,
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
    write_workflow(
        consumer_path,
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
            '    source: "{{file:.crewplane/inputs/review-findings.md}}"',
            "  - id: standards-input",
            "    mode: input",
            '    source: "{{file:.crewplane/inputs/coding-standards.md}}"',
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
    write_workflow(
        workflow_path,
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
    return load_validated_workflow(workflow_path, tmp_path)


COMPOSITION_CASES = [
    pytest.param(
        {
            "case_id": "imported-namespaced-nodes",
            "build_workflow": build_namespaced_import_workflow,
            "snapshot_event_type": "workflow_finished",
            "selected_node_id": "summary.final",
            "expected_fragments": ("auth.plan", "summary.final", "✅"),
        },
        id="imported-namespaced-nodes",
    ),
    pytest.param(
        {
            "case_id": "imported-input-node-label",
            "build_workflow": build_imported_input_workflow,
            "snapshot_event_type": "workflow_finished",
            "selected_node_id": "fix.review-input",
            "expected_fragments": ("fix.review-input", "fix.implement", "input", "✅"),
        },
        id="imported-input-node-label",
    ),
    pytest.param(
        {
            "case_id": "bound-import-input-rewrite",
            "build_workflow": build_bound_input_rewrite_workflow,
            "snapshot_event_type": "workflow_finished",
            "selected_node_id": "fix.implement",
            "expected_fragments": (
                "quality.review.findings",
                "fix.standards-input",
                "fix.implement",
                "handoff",
                "input",
                "✅",
            ),
            "unexpected_fragments": ("fix.review-input",),
        },
        id="bound-import-input-rewrite",
    ),
]


@pytest.mark.parametrize("case_data", COMPOSITION_CASES)
def test_render_dag_summary_matches_composed_workflows(
    tmp_path: Path,
    run_visualization_case,
    case_data: dict[str, object],
) -> None:
    run_result = run_visualization_case(tmp_path, case_data)

    for fragment in case_data.get("expected_fragments", ()):
        assert fragment in run_result.rendered
    for fragment in case_data.get("unexpected_fragments", ()):
        assert fragment not in run_result.rendered

    if case_data["case_id"] == "bound-import-input-rewrite":
        standards_input_file = (
            run_result.stages_dir / "fix.standards-input" / "input_round1.md"
        )
        assert standards_input_file.exists()

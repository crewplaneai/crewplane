import tempfile
from pathlib import Path

from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.loading import load_tasks_with_sources
from crewplane.core.workflow.models import (
    WorkflowNode,
    render_prompt_for_role,
)
from crewplane.core.workflow.validation import (
    validate_workflow_plan,
)
from crewplane.version import SCHEMA_VERSION
from tests.unit.core.workflow_composition.workflow_composition_imports_support import (
    write_import_workflow,
)


def _executor_prompt(node: WorkflowNode) -> str:
    return render_prompt_for_role(node, PromptSegmentRole.EXECUTOR)


def test_composes_imported_workflow_and_rewrites_references() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        module = root / "module.task.md"
        workflow = root / "root.task.md"

        write_import_workflow(
            module,
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
                "Build {{param:module_name}} for {{var:project_name}}.",
            ],
        )
        write_import_workflow(
            workflow,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
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

    assert [node.id for node in validated.nodes] == ["auth.plan", "summary.final"]
    assert "payments-auth" in _executor_prompt(validated.nodes[0])
    assert "{{var:project_name}}" in _executor_prompt(validated.nodes[0])
    assert [record.path.name for record in load_result.referenced_workflows] == [
        "root.task.md",
        "module.task.md",
    ]


def test_composes_imported_workflow_and_preserves_findings_references() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        module = root / "module.task.md"
        workflow = root / "root.task.md"

        write_import_workflow(
            module,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
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
        write_import_workflow(
            workflow,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
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

    assert [node.id for node in validated.nodes] == ["quality.review", "implement"]
    assert "{{quality.review.findings}}" in _executor_prompt(validated.nodes[1])


def test_unbound_param_template_rewrites_to_var_template() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        module = root / "module.task.md"
        workflow = root / "root.task.md"

        write_import_workflow(
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
                "Use {{param:module_name}}.",
            ],
        )
        write_import_workflow(
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
                "done",
            ],
        )

        workflow_plan = load_tasks_with_sources(
            workflow,
            project_root=root,
        ).workflow

    assert "{{var:module_name}}" in _executor_prompt(workflow_plan.nodes[0])


def test_nested_imports_compose_alias_chain() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        shared = root / "shared.task.md"
        module = root / "module.task.md"
        workflow = root / "root.task.md"

        write_import_workflow(
            shared,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
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
        write_import_workflow(
            module,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
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
        write_import_workflow(
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
    assert ids == ["auth.shared.normalize", "auth.finalize", "summary"]
    assert workflow_plan.nodes[1].needs == ["auth.shared.normalize"]
    assert "{{auth.shared.normalize.output}}" in _executor_prompt(
        workflow_plan.nodes[1]
    )


def test_root_node_can_feed_imported_namespace_dependency() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        review = root / "review.task.md"
        fixer = root / "fix.task.md"
        workflow = root / "root.task.md"

        write_import_workflow(
            review,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
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
        write_import_workflow(
            fixer,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
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
        write_import_workflow(
            workflow,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
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

    assert [node.id for node in validated.nodes] == [
        "quality.findings",
        "fix.implement",
        "fix.review-input",
    ]
    assert validated.nodes[1].needs == ["fix.review-input"]
    assert "{{fix.review-input.output}}" in _executor_prompt(validated.nodes[1])

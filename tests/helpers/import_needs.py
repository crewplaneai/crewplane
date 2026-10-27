"""Real Markdown inputs for import prerequisite boundary tests."""

from pathlib import Path

import yaml

from crewplane.version import SCHEMA_VERSION


def write_import_needs_workflow(
    root: Path, prerequisites: tuple[str, ...] = ("plan", "implement")
) -> Path:
    workflow_dir = root / ".crewplane" / "workflows"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (root / "context.md").write_text(
        "Existing documentation.\n", encoding="utf-8", newline="\n"
    )
    (workflow_dir / "child.task.md").write_text(
        f'---\nschema_version: "{SCHEMA_VERSION}"\n'
        "name: Documentation review\n"
        "nodes:\n"
        "  - {id: context, mode: input, source: '{{file:context.md}}'}\n"
        "  - {id: review, mode: sequential, providers: [alpha], needs: [context]}\n"
        "  - {id: audit, mode: sequential, providers: [alpha]}\n"
        "  - {id: summarize, mode: sequential, providers: [alpha], needs: [review, audit]}\n"
        "---\n"
        "## review\nReview {{context.output}}.\n"
        "## audit\nAudit documentation independently.\n"
        "## summarize\nSummarize the documentation review.\n",
        encoding="utf-8",
    )
    workflow = workflow_dir / "root.task.md"
    workflow.write_text(
        f'---\nschema_version: "{SCHEMA_VERSION}"\n'
        "name: Import prerequisites\n"
        "imports:\n"
        "  - path: child.task.md\n"
        "    as: doc\n"
        f"    needs: {yaml.safe_dump(list(prerequisites), default_flow_style=True).strip()}\n"
        "nodes:\n"
        "  - {id: plan, mode: sequential, providers: [alpha]}\n"
        "  - {id: implement, mode: sequential, providers: [alpha], needs: [plan]}\n"
        "---\n"
        "## plan\nWrite the implementation plan.\n"
        "## implement\nImplement the plan.\n",
        encoding="utf-8",
    )
    return workflow

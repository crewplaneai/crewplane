import os
from pathlib import Path

from orchestrator_cli.core.versions import (
    CONFIG_SCHEMA_VERSION,
    WORKFLOW_SCHEMA_VERSION,
)


def project_pythonpath() -> str:
    project_root = Path(__file__).resolve().parent.parent
    existing_pythonpath = os.environ.get("PYTHONPATH")
    return os.pathsep.join(
        part for part in (str(project_root / "src"), existing_pythonpath) if part
    )


def write_basic_config(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                f'version: "{CONFIG_SCHEMA_VERSION}"',
                "",
                "agents:",
                "  alpha:",
                '    cli_cmd: ["echo"]',
                '    default_model: "model-a"',
                "  beta:",
                '    cli_cmd: ["echo"]',
                '    default_model: "model-b"',
            ]
        ),
        encoding="utf-8",
    )


def write_basic_config_without_default_model(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                f'version: "{CONFIG_SCHEMA_VERSION}"',
                "",
                "agents:",
                "  alpha:",
                '    cli_cmd: ["echo"]',
            ]
        ),
        encoding="utf-8",
    )


def write_basic_config_with_settings(path: Path, log_cli_output: bool) -> None:
    path.write_text(
        "\n".join(
            [
                f'version: "{CONFIG_SCHEMA_VERSION}"',
                "",
                "agents:",
                "  alpha:",
                '    cli_cmd: ["echo"]',
                '    default_model: "model-a"',
                "settings:",
                "  integrations:",
                "    artifacts:",
                '      implementation: "filesystem"',
                "      options:",
                f"        log_cli_output: {'true' if log_cli_output else 'false'}",
                "        allowed_template_paths: []",
            ]
        ),
        encoding="utf-8",
    )


def repo_task_workflow_stage_names() -> set[str]:
    stages_root = (
        Path(__file__).resolve().parent.parent / ".orchestrator" / "execution-stages"
    )
    if not stages_root.exists():
        return set()
    return {
        path.name
        for path in stages_root.iterdir()
        if path.is_dir()
        and (path.name.startswith("task-") or path.name.startswith("workflow-"))
    }


def write_basic_workflow(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                "name: Task",
                "description: markdown workflow",
                "nodes:",
                "  - id: review.node",
                "    mode: sequential",
                "    providers:",
                "      - provider: alpha",
                "        role: executor",
                "---",
                "",
                "## review.node",
                "",
                "Do review.",
            ]
        ),
        encoding="utf-8",
    )


def write_basic_workflow_with_provider_model(path: Path, model: str) -> None:
    path.write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                "name: Task",
                "description: markdown workflow",
                "nodes:",
                "  - id: review.node",
                "    mode: sequential",
                "    providers:",
                "      - provider: alpha",
                f'        model: "{model}"',
                "        role: executor",
                "---",
                "",
                "## review.node",
                "",
                "Do review.",
            ]
        ),
        encoding="utf-8",
    )


def write_review_workflow(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                "name: Task",
                "nodes:",
                "  - id: review.node",
                "    mode: sequential",
                "    depth: 1",
                "    providers:",
                "      - provider: alpha",
                "        role: executor",
                "      - provider: beta",
                "        role: reviewer",
                "---",
                "",
                "## review.node",
                "",
                "Review this.",
            ]
        ),
        encoding="utf-8",
    )


def write_workflow_with_name(path: Path, workflow_name: str) -> None:
    path.write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
                f"name: {workflow_name}",
                "description: markdown workflow",
                "nodes:",
                "  - id: review.node",
                "    mode: sequential",
                "    providers:",
                "      - provider: alpha",
                "        role: executor",
                "---",
                "",
                "## review.node",
                "",
                "Do review.",
            ]
        ),
        encoding="utf-8",
    )

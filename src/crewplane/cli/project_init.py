from __future__ import annotations

from pathlib import Path

from rich.console import Console

from crewplane.core.preflight.secrets import FingerprintKeyProvider

from .paths import WORKFLOWS_DIRNAME, ensure_state_dir
from .templates import (
    CONFIG_TEMPLATE,
    DEFAULT_WORKFLOW_TEMPLATE,
    WORKFLOW_LIBRARY_TEMPLATE_DIR,
    create_template_file,
    discover_workflow_library_assets,
)


def initialize_project_templates(
    console: Console, project_root: Path | None = None
) -> Path:
    state_dir = ensure_state_dir(project_root)
    workflows_dir = state_dir / WORKFLOWS_DIRNAME
    workflows_dir.mkdir(exist_ok=True)
    workflow_library_dir = workflows_dir / "example-templates"
    workflow_library_dir.mkdir(exist_ok=True)

    create_template_file(state_dir / "config.yml", CONFIG_TEMPLATE, console)
    create_template_file(
        workflows_dir / "single-agent-review.task.md",
        DEFAULT_WORKFLOW_TEMPLATE,
        console,
    )
    install_template_assets(
        WORKFLOW_LIBRARY_TEMPLATE_DIR,
        workflow_library_dir,
        discover_workflow_library_assets(),
        console,
    )
    key_result = FingerprintKeyProvider(state_dir).load_key("persist_if_needed")
    for diagnostic in key_result.diagnostics:
        console.print(f"[yellow]WARN[/] {diagnostic.message}")
    return state_dir


def install_template_assets(
    source_root: Path,
    destination_root: Path,
    assets: list[Path],
    console: Console,
) -> None:
    for relative_path in assets:
        source_template = source_root / relative_path
        output_path = destination_root / relative_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        create_template_file(output_path, source_template, console)

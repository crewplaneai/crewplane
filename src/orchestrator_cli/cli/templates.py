from pathlib import Path

import typer
from rich.console import Console

from orchestrator_cli.version import SCHEMA_VERSION

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "example_templates"
CONFIG_TEMPLATE = TEMPLATE_DIR / "config.yml"
DEFAULT_WORKFLOW_TEMPLATE = TEMPLATE_DIR / "code-review-example.task.md"
WORKFLOW_LIBRARY_TEMPLATE_DIR = TEMPLATE_DIR / "example-templates"
TEMPLATE_VERSION_TOKENS = {
    "__SCHEMA_VERSION__": SCHEMA_VERSION,
}

__all__ = [
    "CONFIG_TEMPLATE",
    "DEFAULT_WORKFLOW_TEMPLATE",
    "TEMPLATE_DIR",
    "TEMPLATE_VERSION_TOKENS",
    "WORKFLOW_LIBRARY_TEMPLATE_DIR",
    "create_template_file",
    "discover_workflow_library_assets",
    "discover_workflow_library_templates",
    "render_template_content",
    "write_template",
]


def render_template_content(template_content: str) -> str:
    rendered = template_content
    for token, value in TEMPLATE_VERSION_TOKENS.items():
        rendered = rendered.replace(token, value)
    return rendered


def write_template(target_path: Path, template_path: Path, console: Console) -> None:
    try:
        template_content = template_path.read_text(encoding="utf-8")
        target_path.write_text(
            render_template_content(template_content), encoding="utf-8"
        )
    except FileNotFoundError as exc:
        console.print(f"[red]Error:[/] Missing template file: {template_path}")
        raise typer.Exit(code=1) from exc


def create_template_file(
    file_path: Path, template_path: Path, console: Console
) -> None:
    if file_path.exists():
        console.print(f"[yellow]⚠[/] {file_path} already exists")
        return
    write_template(file_path, template_path, console)
    console.print(f"[green]✓[/] Created {file_path}")


def discover_workflow_library_templates() -> list[Path]:
    if not WORKFLOW_LIBRARY_TEMPLATE_DIR.exists():
        return []
    return sorted(
        path.relative_to(WORKFLOW_LIBRARY_TEMPLATE_DIR)
        for path in WORKFLOW_LIBRARY_TEMPLATE_DIR.rglob("*.task.md")
        if path.is_file()
    )


def discover_workflow_library_assets() -> list[Path]:
    if not WORKFLOW_LIBRARY_TEMPLATE_DIR.exists():
        return []
    return sorted(
        path.relative_to(WORKFLOW_LIBRARY_TEMPLATE_DIR)
        for path in WORKFLOW_LIBRARY_TEMPLATE_DIR.rglob("*")
        if path.is_file()
    )

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from orchestrator_cli.cli.app import app
from orchestrator_cli.version import SCHEMA_VERSION


def assert_cleanup_dry_run(project_root: Path, cache_root: Path) -> None:
    config_path = project_root / ".orchestrator" / "cleanup-config.yml"
    config_path.write_text(
        "\n".join(
            [
                f'version: "{SCHEMA_VERSION}"',
                "agents:",
                "  alpha:",
                '    cli_cmd: ["mock"]',
                '    default_model: "model-a"',
                "settings:",
                "  workspace:",
                "    enabled: true",
                f'    cache_root: "{cache_root.as_posix()}"',
                "    cleanup_on_success: false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--dry-run"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "Would remove" in result.output
    assert "workspace path(s)" in result.output
    assert (cache_root / "workspaces").exists()
    assert (cache_root / "snapshots").exists()
    assert (cache_root / "review-workspaces").exists()

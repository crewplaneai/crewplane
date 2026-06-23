from __future__ import annotations

import io
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.console import Console

import crewplane.cli.app as cli
from crewplane.architecture.contracts import CanonicalIntegrationConfig
from crewplane.artifacts.naming import build_run_key_name
from crewplane.cli.run.preflight import (
    compile_workflow_preview,
    raise_for_preflight_preview_errors,
)
from crewplane.core.config import load_config
from crewplane.core.execution_state import RunManifest
from crewplane.core.preflight import PreflightCompilationPreview
from crewplane.core.preflight.source import load_workflow_source_for_preflight
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import make_run_manifest, write_run_manifest
from tests.integration.cli.cli_workflow_helpers import (
    ConsoleFactory,
    write_basic_config,
    write_basic_workflow,
)


class DryRunUnavailableArtifactsAdapter:
    create_store_calls = 0

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: dict[str, Any] | None = None,
    ) -> CanonicalIntegrationConfig:
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            options=dict(options or {}),
        )

    def create_store(
        self,
        workflow_name: str,
        state_dir: Path,
        project_root: Path,
        options: dict[str, Any] | None = None,
    ) -> None:
        type(self).create_store_calls += 1
        raise AssertionError(
            "dry-run must not create an artifact store: "
            f"{workflow_name}, {state_dir}, {project_root}, {options}"
        )


def write_standard_project(
    root: Path,
    config_writer: Callable[[Path], None] = write_basic_config,
    workflow_writer: Callable[[Path], None] = write_basic_workflow,
) -> tuple[Path, Path]:
    state_dir = root / ".crewplane"
    workflow_dir = state_dir / "workflows"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    config_path = state_dir / "config.yml"
    workflow_path = workflow_dir / "workflow.task.md"
    config_writer(config_path)
    workflow_writer(workflow_path)
    return config_path, workflow_path


def write_nonfilesystem_config(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                f'version: "{SCHEMA_VERSION}"',
                "",
                "agents:",
                "  alpha:",
                '    cli_cmd: ["echo"]',
                '    default_model: "model-a"',
                "settings:",
                "  integrations:",
                "    invoker:",
                '      implementation: "cli"',
                "      options: {}",
                "    ui:",
                '      implementation: "none"',
                "      options: {}",
                "    artifacts:",
                f'      implementation: "{__name__}:DryRunUnavailableArtifactsAdapter"',
                "      options: {}",
            ]
        ),
        encoding="utf-8",
    )


def write_sensitive_env_workflow(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
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
                "Use {{env:API_TOKEN}}.",
            ]
        ),
        encoding="utf-8",
    )


def run_dry_run(
    root: Path,
    config_path: Path,
    workflow_path: Path,
    force: bool = False,
) -> str:
    stream = io.StringIO()
    original_console_cls = cli.Console
    cli.Console = ConsoleFactory(
        file=stream,
        force_terminal=False,
        color_system=None,
        width=120,
    )
    original_cwd = Path.cwd()
    os.chdir(root)
    try:
        cli.run(
            tasks_file=workflow_path,
            config_file=config_path,
            dry_run=True,
            force=force,
        )
    finally:
        os.chdir(original_cwd)
        cli.Console = original_console_cls
    return stream.getvalue()


def compile_preview(
    root: Path,
    config_path: Path,
    workflow_path: Path,
) -> PreflightCompilationPreview:
    console = Console(file=io.StringIO(), force_terminal=False)
    preview = compile_workflow_preview(
        config=load_config(config_path),
        source=load_workflow_source_for_preflight(
            workflow_path,
            project_root=root,
        ),
        console=console,
        no_live=True,
        fingerprint_key_policy="read_only",
        project_root=root,
        state_dir=root / ".crewplane",
    )
    raise_for_preflight_preview_errors(preview, console)
    if preview.workflow_signature is None:
        raise AssertionError("test workflow must compile successfully")
    return preview


def write_run_history(
    root: Path,
    preview: PreflightCompilationPreview,
    workflow_path: Path,
    run_id: str,
    status: str,
) -> RunManifest:
    workflow_identity = workflow_path.relative_to(root).as_posix()
    workflow_name = preview.workflow_name
    workflow_signature = preview.workflow_signature
    if workflow_name is None or workflow_signature is None:
        raise AssertionError("test preview must include workflow identity")
    manifest = make_run_manifest(
        run_id=run_id,
        run_key_name=build_run_key_name(workflow_name, run_id),
        status=status,
        workflow_identity=workflow_identity,
        workflow_name=workflow_name,
        workflow_signature=workflow_signature,
    )
    write_run_manifest(root / ".crewplane", manifest)
    return manifest


def artifact_tree(state_dir: Path) -> tuple[tuple[str, bytes], ...]:
    entries: list[tuple[str, bytes]] = []
    for root_name in ("locks", "execution-stages", "execution-results", "preflight"):
        root = state_dir / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            relative_path = path.relative_to(state_dir).as_posix()
            if path.is_dir():
                entries.append((f"{relative_path}/", b""))
            elif path.is_file():
                entries.append((relative_path, path.read_bytes()))
            else:
                entries.append((relative_path, b"<special>"))
    return tuple(entries)

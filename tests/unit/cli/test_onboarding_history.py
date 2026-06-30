from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from crewplane.artifacts.naming import build_run_key_name
from crewplane.cli.onboarding.history import (
    MOCK_INVOKER_RESOLVED_IDENTITY,
    find_successful_mock_run_evidence,
)
from crewplane.cli.project_init import initialize_project_templates
from crewplane.cli.run.preflight import (
    compile_workflow_preview,
    raise_for_preflight_preview_errors,
)
from crewplane.core.config import load_config
from crewplane.core.preflight import load_workflow_source_for_preflight
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import make_run_manifest, write_run_manifest


def test_successful_mock_manifest_is_authoritative_evidence(tmp_path: Path) -> None:
    config, source, console = initialize_default_project(tmp_path)
    preview = compile_default_preview(tmp_path, config, source, console)
    write_history_manifest(
        tmp_path, preview, "succeeded", MOCK_INVOKER_RESOLVED_IDENTITY
    )

    evidence = find_successful_mock_run_evidence(
        tmp_path,
        tmp_path / ".crewplane",
        config,
        source,
        console,
    )

    assert evidence.found is True
    assert evidence.warning is None


def test_successful_non_mock_manifest_is_rejected(tmp_path: Path) -> None:
    config, source, console = initialize_default_project(tmp_path)
    preview = compile_default_preview(tmp_path, config, source, console)
    write_history_manifest(
        tmp_path,
        preview,
        "succeeded",
        "crewplane.adapters.invokers.cli:CliInvokerAdapter",
    )

    evidence = find_successful_mock_run_evidence(
        tmp_path,
        tmp_path / ".crewplane",
        config,
        source,
        console,
    )

    assert evidence.found is False
    assert evidence.warning is None


def test_unsafe_history_returns_warning_without_traceback(tmp_path: Path) -> None:
    config, source, console = initialize_default_project(tmp_path)
    stages_root = tmp_path / ".crewplane" / "execution-stages"
    stages_root.symlink_to(tmp_path)

    evidence = find_successful_mock_run_evidence(
        tmp_path,
        tmp_path / ".crewplane",
        config,
        source,
        console,
    )

    assert evidence.found is False
    assert evidence.warning is not None
    assert "symlink" in evidence.warning


def initialize_default_project(tmp_path: Path):
    console = Console(file=io.StringIO(), force_terminal=False, color_system=None)
    initialize_project_templates(console, tmp_path)
    config = load_config(tmp_path / ".crewplane" / "config.yml")
    source = load_workflow_source_for_preflight(
        tmp_path / ".crewplane" / "workflows" / "single-agent-review.task.md",
        project_root=tmp_path,
    )
    return config, source, console


def compile_default_preview(tmp_path: Path, config, source, console):
    preview = compile_workflow_preview(
        config=config,
        source=source,
        console=console,
        no_live=True,
        fingerprint_key_policy="read_only",
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        check_cli_availability=False,
    )
    raise_for_preflight_preview_errors(preview, console)
    assert preview.workflow_name is not None
    assert preview.workflow_signature is not None
    return preview


def write_history_manifest(
    tmp_path: Path,
    preview,
    status: str,
    invoker_identity: str,
) -> None:
    run_key_name = build_run_key_name(preview.workflow_name, "history-run")
    manifest = make_run_manifest(
        run_id="history-run",
        run_key_name=run_key_name,
        status=status,
        workflow_identity=".crewplane/workflows/single-agent-review.task.md",
        workflow_name=preview.workflow_name,
        workflow_signature=preview.workflow_signature,
    )
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "invoker": {
            "implementation": "mock",
            "resolved_identity": invoker_identity,
            "options": {},
        },
    }
    write_run_manifest(
        tmp_path / ".crewplane",
        manifest.model_copy(update={"runtime_config_snapshot": snapshot}),
    )

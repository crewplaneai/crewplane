from __future__ import annotations

import json
from pathlib import Path
from typing import Never
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

import crewplane.cli.cleanup as cleanup_cli
import crewplane.cli.workspace_cleanup.eligibility as cleanup_eligibility
import crewplane.cli.workspace_cleanup.execution as cleanup_execution
import crewplane.cli.workspace_cleanup.run_artifacts as cleanup_run_artifacts
import crewplane.runtime.workspace.cleanup as workspace_cleanup
from crewplane.cli.app import app
from crewplane.cli.workspace_cleanup_evidence import WorkspaceCleanupEvidence
from crewplane.core.preflight.models import PreflightExecutionPlan
from tests.helpers.resume import (
    make_run_manifest,
)
from tests.unit.cli.cleanup_support import (
    cleanup_project,
    resolve_workspace_cleanup_context,
)


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError("read-only cache"),
        OSError("workspace I/O failure"),
    ],
)
def test_cleanup_workspaces_reports_mutation_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
) -> None:
    _, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
    )

    def fail_cleanup(
        context: object,
        destructive: bool,
    ) -> Never:
        del context, destructive
        raise failure

    monkeypatch.setattr(cleanup_cli, "execute_workspace_cleanup", fail_cleanup)

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
    )

    assert result.exit_code == 1
    assert f"Cleanup failed: {failure}" in result.output
    assert not isinstance(result.exception, OSError)
    assert workspace_path.exists()


def test_execute_workspace_cleanup_preview_never_refreshes_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, config_path, _ = cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    _write_cleanup_node_manifest(project_root, "run-1")
    refresh = Mock(side_effect=AssertionError("preview cleanup refreshed descriptors"))
    monkeypatch.setattr(
        cleanup_run_artifacts,
        "refresh_node_workspace_descriptor",
        refresh,
    )

    context = resolve_workspace_cleanup_context(config_path)
    result = cleanup_cli.execute_workspace_cleanup(context, destructive=False)

    assert result.selected_count == 1
    refresh.assert_not_called()


def test_execute_workspace_cleanup_refreshes_sorted_union_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    _write_cleanup_node_manifest(project_root, "run-1")
    _copy_cleanup_run(project_root, "a-run")
    _copy_cleanup_run(project_root, "z-run")
    cache_root = workspace_path.parents[3]
    expected = _removed_cleanup_result(cache_root, "z-run", "a-run")
    monkeypatch.setattr(
        cleanup_execution,
        "cleanup_workspace_cache",
        Mock(return_value=expected),
    )
    refresh_calls: list[str] = []

    def record_refresh(
        node: object,
        plan: PreflightExecutionPlan,
        store: object,
    ) -> None:
        del node, store
        refresh_calls.append(plan.run_key_name)

    monkeypatch.setattr(
        cleanup_run_artifacts,
        "refresh_node_workspace_descriptor",
        record_refresh,
    )

    context = resolve_workspace_cleanup_context(config_path)
    assert cleanup_cli.execute_workspace_cleanup(context, destructive=True) is expected
    assert refresh_calls == ["a-run", "run-1", "z-run"]


def test_execute_workspace_cleanup_preserves_failure_and_notes_refresh_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, config_path, _ = cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    _write_cleanup_node_manifest(project_root, "run-1")
    cleanup_error = ValueError("cleanup failed")
    cleanup_error.add_note("existing cleanup note")
    monkeypatch.setattr(
        cleanup_execution,
        "cleanup_workspace_cache",
        Mock(side_effect=cleanup_error),
    )
    refresh_calls: list[str] = []

    def fail_refresh(
        node: object,
        plan: PreflightExecutionPlan,
        store: object,
    ) -> Never:
        del node, store
        refresh_calls.append(plan.run_key_name)
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(
        cleanup_run_artifacts,
        "refresh_node_workspace_descriptor",
        fail_refresh,
    )

    context = resolve_workspace_cleanup_context(config_path)
    with pytest.raises(ValueError, match="cleanup failed") as exc_info:
        cleanup_cli.execute_workspace_cleanup(context, destructive=True)

    assert exc_info.value is cleanup_error
    assert exc_info.value.__notes__ == [
        "existing cleanup note",
        "Workspace descriptor refresh after partial cleanup failed: refresh failed",
    ]
    assert refresh_calls == ["run-1"]


@pytest.mark.parametrize(
    ("failure_stage", "expected_message", "expected_cause_type"),
    [
        (
            "manifest",
            "Cannot refresh workspace descriptors for 'run-1'.",
            None,
        ),
        ("plan_location", "Preflight plan is missing for run 'run-1'.", None),
        ("plan_content", "Preflight plan is invalid for run 'run-1'.", ValueError),
        ("identity", "Preflight plan identity is invalid for run 'run-1'.", None),
    ],
)
def test_load_cleanup_preflight_plan_preserves_first_failure_and_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    expected_message: str,
    expected_cause_type: type[BaseException] | None,
) -> None:
    project_root, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    _write_cleanup_node_manifest(project_root, "run-1")
    run_dir = project_root / ".crewplane/execution-stages/run-1"
    manifest_path = run_dir / "manifests/run.json"
    plan_path = run_dir / "preflight/execution-plan.json"
    if failure_stage == "manifest":
        manifest_path.write_text("{}", encoding="utf-8")
        plan_path.unlink()
    elif failure_stage == "plan_location":
        plan_path.unlink()
    elif failure_stage == "plan_content":
        plan_path.write_text("{}", encoding="utf-8")
    else:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
        payload["run_id"] = "other-run"
        plan_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        cleanup_execution,
        "cleanup_workspace_cache",
        Mock(
            return_value=_removed_cleanup_result(
                workspace_path.parents[3],
                "run-1",
            )
        ),
    )

    context = resolve_workspace_cleanup_context(config_path)
    with pytest.raises(RuntimeError) as exc_info:
        cleanup_cli.execute_workspace_cleanup(context, destructive=True)

    assert str(exc_info.value) == expected_message
    if expected_cause_type is None:
        assert exc_info.value.__cause__ is None
    else:
        assert isinstance(exc_info.value.__cause__, expected_cause_type)


@pytest.mark.parametrize(
    ("manifest_kind", "expected_reason"),
    [
        ("invalid", "run manifest is invalid"),
        ("mismatched", "run manifest is active or mismatched"),
        ("running", "run manifest is active or mismatched"),
    ],
)
def test_workspace_manifest_rejection_precedes_activity_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_kind: str,
    expected_reason: str,
) -> None:
    project_root, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
        create_workspace=False,
    )
    workspace_path.mkdir(parents=True)
    (workspace_path / "orphan.txt").write_text("orphan", encoding="utf-8")
    state_dir = project_root / ".crewplane"
    manifest_path = state_dir / "execution-stages/run-1/manifests/run.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        _workspace_manifest_content(manifest_kind),
        encoding="utf-8",
    )
    evidence = Mock(spec=WorkspaceCleanupEvidence)
    evidence.ref_cleanup_run_keys.return_value = ("run-1",)
    evidence.absent_state_projections.return_value = ()
    evidence.status_for_cache_key.return_value = None
    evidence.decision.return_value = None
    monkeypatch.setattr(
        cleanup_execution,
        "WorkspaceCleanupEvidence",
        Mock(return_value=evidence),
    )
    activity_failure = AssertionError("manifest rejection must precede activity checks")
    run_lock = Mock(side_effect=activity_failure)
    provider_processes = Mock(side_effect=activity_failure)
    monkeypatch.setattr(cleanup_eligibility, "run_lock_activity", run_lock)
    monkeypatch.setattr(
        cleanup_eligibility,
        "ensure_no_live_provider_processes",
        provider_processes,
    )

    context = resolve_workspace_cleanup_context(config_path, orphans=True)
    result = cleanup_cli.execute_workspace_cleanup(context, destructive=False)

    assert len(result.entries) == 1
    assert result.entries[0].retained_reason == expected_reason
    run_lock.assert_not_called()
    provider_processes.assert_not_called()


def _write_cleanup_node_manifest(project_root: Path, run_key_name: str) -> None:
    node_manifest_path = (
        project_root
        / ".crewplane/execution-stages"
        / run_key_name
        / "manifests/nodes/node.json"
    )
    node_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    node_manifest_path.write_text("{}", encoding="utf-8")


def _copy_cleanup_run(project_root: Path, run_key_name: str) -> None:
    source_run_dir = project_root / ".crewplane/execution-stages/run-1"
    target_run_dir = project_root / ".crewplane/execution-stages" / run_key_name
    manifest = json.loads(
        (source_run_dir / "manifests/run.json").read_text(encoding="utf-8")
    )
    manifest.update({"run_id": run_key_name, "run_key_name": run_key_name})
    manifest_path = target_run_dir / "manifests/run.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    plan = json.loads(
        (source_run_dir / "preflight/execution-plan.json").read_text(encoding="utf-8")
    )
    plan.update({"run_id": run_key_name, "run_key_name": run_key_name})
    plan_path = target_run_dir / "preflight/execution-plan.json"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    _write_cleanup_node_manifest(project_root, run_key_name)


def _removed_cleanup_result(
    cache_root: Path,
    *run_key_names: str,
) -> workspace_cleanup.WorkspaceCleanupResult:
    return workspace_cleanup.WorkspaceCleanupResult(
        cache_root=cache_root,
        entries=tuple(
            workspace_cleanup.WorkspaceCleanupEntry(
                path=cache_root / run_key_name,
                run_key_name=run_key_name,
                size_bytes=0,
                removed=True,
                status="succeeded",
                orphan=False,
            )
            for run_key_name in run_key_names
        ),
    )


def _workspace_manifest_content(manifest_kind: str) -> str:
    if manifest_kind == "invalid":
        return "{}"
    if manifest_kind == "mismatched":
        return make_run_manifest(
            run_id="other-run",
            run_key_name="other-run",
            status="succeeded",
        ).model_dump_json()
    if manifest_kind == "running":
        return make_run_manifest(
            run_id="run-1",
            run_key_name="run-1",
            status="running",
        ).model_dump_json()
    raise AssertionError(f"Unsupported manifest kind: {manifest_kind}")

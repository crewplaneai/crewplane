from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from crewplane.architecture.contracts import CanonicalIntegrationConfig
from crewplane.artifacts.resume.decision import ResumeDecision
from crewplane.artifacts.run_history import RunHistoryRecord
from crewplane.cli.run import resume as resume_module
from crewplane.cli.run.resume import (
    ResumePlan,
    build_resume_plan,
    print_dry_run_resume_advisory,
)
from crewplane.core.config import Config, Settings
from crewplane.core.execution_state import RunManifest
from crewplane.core.preflight import (
    PreflightCompilationPreview,
    PreflightExecutionPlan,
)
from crewplane.core.preflight.models import WorkspaceBranchExportRecord
from crewplane.core.preflight.runtime_config import (
    RuntimeConfigSnapshot,
    RuntimeConfigSnapshotOptions,
)
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import (
    WORKFLOW_IDENTITY,
    WORKFLOW_NAME,
    WORKFLOW_SIGNATURE,
    attach_workspace_descriptor,
    make_node_state,
    make_plan,
    make_run_manifest,
    write_node_state,
    write_result,
    write_run_manifest,
)
from tests.helpers.resume_validation import (
    attach_git_workspace_source,
    provider_workspace_state_payload,
    write_lineage_bundle_for_payload,
)
from tests.helpers.workspace_records import workspace_selection_record


def test_force_resume_plan_does_not_scan_unsafe_history(
    tmp_path,
    monkeypatch,
) -> None:
    state_dir = tmp_path / ".crewplane"
    stages_root = state_dir / "execution-stages"
    original_lstat = Path.lstat

    def blocked_history_lstat(self):
        if self == stages_root:
            raise PermissionError("history blocked")
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", blocked_history_lstat)

    resume_plan = build_resume_plan(
        Config(version=SCHEMA_VERSION, agents={}),
        _workflow_source(tmp_path),
        PreflightCompilationPreview(
            workflow_name=WORKFLOW_NAME,
            workflow_signature=WORKFLOW_SIGNATURE,
        ),
        tmp_path,
        state_dir,
        force=True,
    )

    assert resume_plan.decision.kind == "execute_full"


def test_provider_workspace_resume_plan_fails_closed_on_unsafe_history(
    tmp_path,
    monkeypatch,
) -> None:
    state_dir = tmp_path / ".crewplane"
    stages_root = state_dir / "execution-stages"
    original_lstat = Path.lstat

    def blocked_history_lstat(self):
        if self == stages_root:
            raise PermissionError("history blocked")
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", blocked_history_lstat)

    with pytest.raises(PermissionError, match="history blocked"):
        build_resume_plan(
            _workspace_config(),
            _workflow_source(tmp_path),
            _provider_workspace_preview(),
            tmp_path,
            state_dir,
            force=False,
        )


def test_dry_run_resume_advisory_reports_unsafe_history_as_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    state_dir = tmp_path / ".crewplane"
    stages_root = state_dir / "execution-stages"
    original_lstat = Path.lstat

    def blocked_history_lstat(self):
        if self == stages_root:
            raise PermissionError("history blocked")
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", blocked_history_lstat)
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None, width=200)

    print_dry_run_resume_advisory(
        _workspace_config(),
        _workflow_source(tmp_path),
        _provider_workspace_preview(),
        tmp_path,
        state_dir,
        force=False,
        console=console,
    )

    assert "Resume advisory: unavailable (history blocked)" in stream.getvalue()


def test_resume_plan_rejects_non_filesystem_artifacts_for_real_run(tmp_path) -> None:
    state_dir = tmp_path / ".crewplane"

    with pytest.raises(
        RuntimeError,
        match="Real execution requires the built-in filesystem artifacts backend",
    ):
        build_resume_plan(
            _nonfilesystem_config(),
            _workflow_source(tmp_path),
            PreflightCompilationPreview(
                workflow_name=WORKFLOW_NAME,
                workflow_signature=WORKFLOW_SIGNATURE,
            ),
            tmp_path,
            state_dir,
            force=False,
        )

    assert not (state_dir / "locks").exists()


def test_workspace_enabled_resume_plan_skips_valid_success(tmp_path) -> None:
    state_dir = tmp_path / ".crewplane"
    plan = make_plan()
    preview = _preview_from_plan(plan)
    manifest = make_run_manifest("success", "workflow--success", status="succeeded")
    write_run_manifest(state_dir, manifest)
    source_run_dir = state_dir / "execution-stages" / manifest.run_key_name
    source_results_dir = state_dir / "execution-results" / manifest.run_key_name
    for node in plan.nodes:
        descriptor = write_result(
            source_results_dir,
            node.artifact_contract.output_path,
            f"{node.id} output",
        )
        write_node_state(
            source_run_dir, make_node_state(manifest, node.id, [descriptor])
        )

    resume_plan = build_resume_plan(
        _workspace_config(),
        _workflow_source(tmp_path),
        preview,
        tmp_path,
        state_dir,
        force=False,
    )

    assert resume_plan.decision.kind == "skip"
    assert resume_plan.decision.successful_run is not None
    assert resume_plan.decision.successful_run.manifest.run_id == "success"


def test_workspace_enabled_project_root_preview_keeps_manifest_only_skip(
    tmp_path,
    monkeypatch,
) -> None:
    state_dir = tmp_path / ".crewplane"
    plan = make_plan()
    preview = _preview_from_plan(plan)
    manifest = make_run_manifest("success", "workflow--success", status="succeeded")
    write_run_manifest(state_dir, manifest)

    def reject_workspace_frontier_validation(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError(
            "project-root duplicate skip must not use workspace resume"
        )

    monkeypatch.setattr(
        resume_module,
        "validate_resume_frontier",
        reject_workspace_frontier_validation,
    )

    resume_plan = build_resume_plan(
        _workspace_config(),
        _workflow_source(tmp_path),
        preview,
        tmp_path,
        state_dir,
        force=False,
    )

    assert resume_plan.decision.kind == "skip"
    assert resume_plan.decision.successful_run is not None
    assert resume_plan.decision.successful_run.manifest.run_id == "success"


def test_workspace_enabled_resume_plan_skips_branch_only_change(tmp_path) -> None:
    state_dir = tmp_path / ".crewplane"
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    plan = plan.model_copy(update={"nodes": [node, plan.nodes[1]]})
    plan, repo = attach_git_workspace_source(tmp_path, plan)
    current_policy = policy.model_copy(
        update={
            "branch_export": WorkspaceBranchExportRecord(
                create_branch=True,
                branch_name="ai/new-branch",
            )
        }
    )
    current_plan = plan.model_copy(
        update={
            "nodes": [
                node.model_copy(update={"workspace_policy": current_policy}),
                plan.nodes[1],
            ],
        }
    )
    manifest = make_run_manifest("success", "workflow--success", status="succeeded")
    write_run_manifest(state_dir, manifest)
    source_run_dir = state_dir / "execution-stages" / manifest.run_key_name
    source_results_dir = state_dir / "execution-results" / manifest.run_key_name
    a_descriptor = write_result(source_results_dir, "a-result.md", "a output")
    b_descriptor = write_result(source_results_dir, "b-result.md", "b output")
    write_node_state(
        source_run_dir,
        make_node_state(manifest, "a", [a_descriptor]),
    )
    write_node_state(
        source_run_dir,
        make_node_state(manifest, "b", [b_descriptor]),
    )
    assert plan.workspace_source is not None
    history = _history_record(manifest, state_dir)
    payload = provider_workspace_state_payload(
        history,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
    )
    write_lineage_bundle_for_payload(repo, history, payload)
    state_path = source_run_dir / "a" / "workspace-state.json"
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    attach_workspace_descriptor(source_run_dir, plan, "a")
    payload["branch_export"] = {
        "status": "created",
        "operation": "created",
        "branch_name": "ai/old-branch",
        "branch_ref": "refs/heads/ai/old-branch",
        "record_artifact": "workspace-exports/primary.json",
        "result_commit": "b" * 40,
        "result_tree": "d" * 40,
        "completed_at": "2026-06-16T12:00:00",
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    resume_plan = build_resume_plan(
        _workspace_config(),
        _workflow_source(tmp_path),
        _workspace_preview_from_plan(current_plan),
        tmp_path,
        state_dir,
        force=False,
    )

    assert resume_plan.decision.kind == "skip"


def test_workspace_enabled_resume_plan_reexecutes_missing_result_artifact(
    tmp_path,
) -> None:
    state_dir = tmp_path / ".crewplane"
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    plan = plan.model_copy(update={"nodes": [node, plan.nodes[1]]})
    plan, _repo = attach_git_workspace_source(tmp_path, plan)
    preview = _workspace_preview_from_plan(plan)
    manifest = make_run_manifest("success", "workflow--success", status="succeeded")
    write_run_manifest(state_dir, manifest)
    source_run_dir = state_dir / "execution-stages" / manifest.run_key_name
    source_results_dir = state_dir / "execution-results" / manifest.run_key_name
    first_node = plan.nodes[0]
    descriptor = write_result(
        source_results_dir,
        first_node.artifact_contract.output_path,
        "a output",
    )
    write_node_state(
        source_run_dir,
        make_node_state(manifest, first_node.id, [descriptor]),
    )

    resume_plan = build_resume_plan(
        _workspace_config(),
        _workflow_source(tmp_path),
        preview,
        tmp_path,
        state_dir,
        force=False,
    )

    assert resume_plan.decision.kind == "execute_full"


def test_dry_run_resume_advisory_prints_branch_export_verification(
    tmp_path,
    monkeypatch,
) -> None:
    manifest = make_run_manifest("success", "workflow--success", status="succeeded")
    run_dir = tmp_path / ".crewplane" / "execution-stages" / manifest.run_key_name
    results_dir = tmp_path / ".crewplane" / "execution-results" / manifest.run_key_name
    record = RunHistoryRecord(
        manifest=manifest,
        manifest_path=run_dir / "manifests" / "run.json",
        run_dir=run_dir,
        results_dir=results_dir,
    )

    def build_plan(*args: object, **kwargs: object) -> ResumePlan:
        del args, kwargs
        return ResumePlan(
            workflow_identity=WORKFLOW_IDENTITY,
            decision=ResumeDecision(kind="skip", successful_run=record),
        )

    def preview_branch_exports(
        plan: ResumePlan,
        source: RunHistoryRecord,
    ) -> tuple[dict[str, str], ...]:
        del plan, source
        return (
            {
                "logical_worktree_name": "primary",
                "status": "fulfilled",
                "operation": "created",
                "branch_name": "feature/preview",
            },
        )

    monkeypatch.setattr(resume_module, "build_resume_plan", build_plan)
    monkeypatch.setattr(
        resume_module,
        "preview_branch_exports_from_history",
        preview_branch_exports,
    )
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None, width=200)

    print_dry_run_resume_advisory(
        _workspace_config(),
        _workflow_source(tmp_path),
        _provider_workspace_preview(),
        tmp_path,
        tmp_path / ".crewplane",
        force=False,
        console=console,
    )

    output = stream.getvalue()
    assert "Resume advisory: would_skip" in output
    assert "Branch export verification:" in output
    assert "planned_operation=created" in output
    assert "branch=feature/preview" in output


def test_dry_run_resume_advisory_does_not_skip_failed_branch_export(
    tmp_path,
    monkeypatch,
) -> None:
    manifest = make_run_manifest("success", "workflow--success", status="succeeded")
    run_dir = tmp_path / ".crewplane" / "execution-stages" / manifest.run_key_name
    results_dir = tmp_path / ".crewplane" / "execution-results" / manifest.run_key_name
    record = RunHistoryRecord(
        manifest=manifest,
        manifest_path=run_dir / "manifests" / "run.json",
        run_dir=run_dir,
        results_dir=results_dir,
    )

    def build_plan(*args: object, **kwargs: object) -> ResumePlan:
        del args, kwargs
        return ResumePlan(
            workflow_identity=WORKFLOW_IDENTITY,
            decision=ResumeDecision(kind="skip", successful_run=record),
        )

    def preview_branch_exports(
        plan: ResumePlan,
        source: RunHistoryRecord,
    ) -> tuple[dict[str, str], ...]:
        del plan, source
        return (
            {
                "logical_worktree_name": "primary",
                "status": "failed_verification",
                "operation": "failed_verification",
                "branch_name": "feature/preview",
                "failure_message": "export target drifted",
            },
        )

    monkeypatch.setattr(resume_module, "build_resume_plan", build_plan)
    monkeypatch.setattr(
        resume_module,
        "preview_branch_exports_from_history",
        preview_branch_exports,
    )
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None, width=200)

    print_dry_run_resume_advisory(
        _workspace_config(),
        _workflow_source(tmp_path),
        _provider_workspace_preview(),
        tmp_path,
        tmp_path / ".crewplane",
        force=False,
        console=console,
    )

    output = stream.getvalue()
    assert "Resume advisory: would_skip" not in output
    assert "Resume advisory: unavailable (branch export verification failed)" in output
    assert "status=failed_verification" in output
    assert "failure=export target drifted" in output


def _workflow_source(root: Path) -> PreflightWorkflowSource:
    workflow = WorkflowPlan(
        name=WORKFLOW_NAME,
        nodes=[],
    )
    return PreflightWorkflowSource.from_workflow(
        workflow,
        root_workflow_path=root / ".crewplane" / "workflows" / "workflow.task.md",
    )


def _workspace_config() -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={},
        settings=Settings(workspace={"enabled": True}),
    )


def _nonfilesystem_config() -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={},
        settings=Settings(
            integrations={
                "artifacts": {
                    "implementation": "tests.fake_adapters:MemoryArtifactsAdapter",
                    "options": {},
                }
            }
        ),
    )


def _provider_workspace_preview() -> PreflightCompilationPreview:
    plan = make_plan()
    return PreflightCompilationPreview(
        workflow_name=WORKFLOW_NAME,
        workflow_signature=WORKFLOW_SIGNATURE,
        nodes=[plan.nodes[0]],
        runtime_config_snapshot=_runtime_snapshot(),
        effective_runtime_config_signature="f" * 64,
        fingerprint_metadata={"payload_version": "1"},
    )


def _preview_from_plan(plan: PreflightExecutionPlan) -> PreflightCompilationPreview:
    return PreflightCompilationPreview(
        workflow_name=plan.workflow_name,
        workflow_signature=plan.workflow_signature,
        execution_order=list(plan.execution_order),
        nodes=list(plan.nodes),
        render_plans=list(plan.render_plans),
        static_resources=list(plan.static_resources),
        workspace_file_locators=list(plan.workspace_file_locators),
        token_catalog=list(plan.token_catalog),
        dependency_graph=list(plan.dependency_graph),
        runtime_config_snapshot=_runtime_snapshot(),
        effective_runtime_config_signature=plan.effective_runtime_config_signature,
        workspace_source=plan.workspace_source,
        fingerprint_metadata=dict(plan.fingerprint_metadata),
    )


def _workspace_preview_from_plan(
    plan: PreflightExecutionPlan,
) -> PreflightCompilationPreview:
    preview = _preview_from_plan(plan)
    return preview.model_copy(
        update={"runtime_config_snapshot": _workspace_runtime_snapshot()}
    )


def _history_record(
    manifest: RunManifest,
    state_dir: Path,
) -> RunHistoryRecord:
    run_dir = state_dir / "execution-stages" / manifest.run_key_name
    return RunHistoryRecord(
        manifest=manifest,
        manifest_path=run_dir / "manifests" / "run.json",
        run_dir=run_dir,
        results_dir=state_dir / "execution-results" / manifest.run_key_name,
    )


def _runtime_snapshot() -> RuntimeConfigSnapshot:
    integration = CanonicalIntegrationConfig(
        implementation="filesystem",
        resolved_identity="filesystem",
    )
    return RuntimeConfigSnapshot.build(
        config=_workspace_config(),
        invoker=integration,
        artifacts=integration,
        ui=integration,
        options=RuntimeConfigSnapshotOptions(no_live=True),
    )


def _workspace_runtime_snapshot() -> RuntimeConfigSnapshot:
    invoker = CanonicalIntegrationConfig(
        implementation="mock",
        resolved_identity="mock",
        capabilities={
            "workspace": {
                "honors_cwd": True,
                "launch_mode": "mock_no_child_process",
                "controlled_child_environment": False,
            }
        },
    )
    integration = CanonicalIntegrationConfig(
        implementation="filesystem",
        resolved_identity="filesystem",
    )
    return RuntimeConfigSnapshot.build(
        config=_workspace_config(),
        invoker=invoker,
        artifacts=integration,
        ui=integration,
        options=RuntimeConfigSnapshotOptions(no_live=True),
    )

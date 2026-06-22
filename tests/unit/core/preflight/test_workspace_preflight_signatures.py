from __future__ import annotations

from pathlib import Path

from rich.console import Console

from orchestrator_cli.bootstrap import build_runtime_config_snapshot
from orchestrator_cli.core.config import AgentConfig, Config, Settings
from orchestrator_cli.core.preflight import (
    PreflightCompileOptions,
    PreflightWorkflowSource,
    compile_preflight_preview,
)
from orchestrator_cli.core.preflight.models import WorkspaceSourceSnapshot
from orchestrator_cli.core.preflight.runtime_config import (
    RuntimeWorkspaceSettingsSnapshot,
    workspace_signature_payload,
)
from orchestrator_cli.core.workflow_composition.models import WorkflowSourceRecord
from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.core.workspace_policy import WorktreeContract
from orchestrator_cli.version import SCHEMA_VERSION


def test_branch_export_fields_do_not_change_workflow_signature(
    tmp_path: Path,
) -> None:
    source_snapshot = source_snapshot_for_commit("a" * 40)
    without_export = compile_source_with_source_snapshot(
        tmp_path,
        PreflightWorkflowSource.from_workflow(
            branch_export_workflow(create_branch=False),
            workflow_content="create_branch: false\n",
        ),
        source_snapshot,
    )
    with_export = compile_source_with_source_snapshot(
        tmp_path,
        PreflightWorkflowSource.from_workflow(
            branch_export_workflow(
                create_branch=True,
                branch_name="feature/exported",
            ),
            workflow_content="create_branch: true\nbranch_name: feature/exported\n",
        ),
        source_snapshot,
    )

    assert without_export.diagnostics == []
    assert with_export.diagnostics == []
    assert without_export.workflow_signature is not None
    assert without_export.workflow_signature == with_export.workflow_signature


def test_imported_branch_export_source_hash_does_not_change_workflow_signature(
    tmp_path: Path,
) -> None:
    workflow = branch_export_workflow(
        create_branch=True,
        branch_name="feature/exported",
    )
    source_snapshot = source_snapshot_for_commit("a" * 40)
    first = compile_source_with_source_snapshot(
        tmp_path,
        PreflightWorkflowSource.from_workflow(
            workflow,
            referenced_workflows=[
                WorkflowSourceRecord(tmp_path / "module.task.md", "a" * 64)
            ],
        ),
        source_snapshot,
    )
    second = compile_source_with_source_snapshot(
        tmp_path,
        PreflightWorkflowSource.from_workflow(
            workflow,
            referenced_workflows=[
                WorkflowSourceRecord(tmp_path / "module.task.md", "b" * 64)
            ],
        ),
        source_snapshot,
    )

    assert first.diagnostics == []
    assert second.diagnostics == []
    assert first.workflow_signature is not None
    assert first.workflow_signature == second.workflow_signature


def test_workspace_enabled_gate_without_selected_worktrees_does_not_change_signature(
    tmp_path: Path,
) -> None:
    workflow = project_root_workflow()
    disabled = _compile_workflow(tmp_path, workflow, _config({"enabled": False}))
    enabled = _compile_workflow(tmp_path, workflow, _config({"enabled": True}))

    assert disabled.diagnostics == []
    assert enabled.diagnostics == []
    assert disabled.nodes[0].workspace_policy is None
    assert enabled.nodes[0].workspace_policy is None
    assert disabled.workflow_signature is not None
    assert disabled.workflow_signature == enabled.workflow_signature
    assert (
        disabled.effective_runtime_config_signature
        == enabled.effective_runtime_config_signature
    )


def test_project_root_workflow_excludes_workspace_settings_from_signature(
    tmp_path: Path,
) -> None:
    workflow = project_root_workflow()
    strict = _compile_workflow(
        tmp_path,
        workflow,
        _config(
            {
                "enabled": True,
                "clean_start": "strict",
                "setup_timeout_seconds": 30,
                "setup_profiles": {"bootstrap": {"run": [["uv", "sync"]]}},
            }
        ),
    )
    tracked_only = _compile_workflow(
        tmp_path,
        workflow,
        _config(
            {
                "enabled": True,
                "clean_start": "tracked_only",
                "setup_timeout_seconds": 60,
                "setup_profiles": {
                    "bootstrap": {
                        "run": [["python", "-m", "pip", "install", "-e", "."]]
                    }
                },
            }
        ),
    )

    assert strict.diagnostics == []
    assert tracked_only.diagnostics == []
    assert strict.nodes[0].workspace_policy is None
    assert tracked_only.nodes[0].workspace_policy is None
    assert strict.workflow_signature is not None
    assert strict.workflow_signature == tracked_only.workflow_signature
    assert strict.effective_runtime_config_signature == (
        tracked_only.effective_runtime_config_signature
    )


def test_worktree_none_excludes_workspace_settings_from_signature(
    tmp_path: Path,
) -> None:
    workflow = WorkflowPlan(
        name="explicit project root workflow",
        worktrees={"primary": {"kind": "worktree", "setup_profile": "bootstrap"}},
        nodes=[
            WorkflowNode(
                id="implement",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[PromptSegment(role="shared", content="run")],
                worktree="none",
            )
        ],
    )
    strict = _compile_workflow(
        tmp_path,
        workflow,
        _config(
            {
                "enabled": True,
                "clean_start": "strict",
                "setup_timeout_seconds": 30,
                "setup_profiles": {"bootstrap": {"run": [["uv", "sync"]]}},
            }
        ),
    )
    tracked_only = _compile_workflow(
        tmp_path,
        workflow,
        _config(
            {
                "enabled": True,
                "clean_start": "tracked_only",
                "setup_timeout_seconds": 60,
                "setup_profiles": {
                    "bootstrap": {
                        "run": [["python", "-m", "pip", "install", "-e", "."]]
                    }
                },
            }
        ),
    )

    assert strict.diagnostics == []
    assert tracked_only.diagnostics == []
    assert strict.nodes[0].workspace_policy is None
    assert tracked_only.nodes[0].workspace_policy is None
    assert strict.workflow_signature is not None
    assert strict.workflow_signature == tracked_only.workflow_signature
    assert strict.effective_runtime_config_signature == (
        tracked_only.effective_runtime_config_signature
    )


def test_selected_setup_timeout_changes_workflow_signature(tmp_path: Path) -> None:
    workflow = setup_workflow()
    source_snapshot = source_snapshot_for_commit("a" * 40)
    thirty_seconds = _compile_workflow(
        tmp_path,
        workflow,
        setup_config(setup_timeout_seconds=30),
        source_snapshot,
    )
    sixty_seconds = _compile_workflow(
        tmp_path,
        workflow,
        setup_config(setup_timeout_seconds=60),
        source_snapshot,
    )

    assert thirty_seconds.diagnostics == []
    assert sixty_seconds.diagnostics == []
    assert thirty_seconds.workflow_signature is not None
    assert thirty_seconds.workflow_signature != sixty_seconds.workflow_signature


def test_selected_setup_command_payload_changes_workflow_signature(
    tmp_path: Path,
) -> None:
    workflow = setup_workflow()
    source_snapshot = source_snapshot_for_commit("a" * 40)
    uv_sync = _compile_workflow(
        tmp_path,
        workflow,
        setup_config(setup_commands=[["uv", "sync"]]),
        source_snapshot,
    )
    pip_install = _compile_workflow(
        tmp_path,
        workflow,
        setup_config(setup_commands=[["python", "-m", "pip", "install", "-e", "."]]),
        source_snapshot,
    )

    assert uv_sync.diagnostics == []
    assert pip_install.diagnostics == []
    assert uv_sync.workflow_signature is not None
    assert uv_sync.workflow_signature != pip_install.workflow_signature


def test_cache_root_does_not_change_default_workflow_signature(
    tmp_path: Path,
) -> None:
    workflow = setup_workflow()
    source_snapshot = source_snapshot_for_commit("a" * 40)
    first = _compile_workflow(
        tmp_path,
        workflow,
        setup_config(cache_root="/tmp/orchestrator-cache-a"),
        source_snapshot,
    )
    second = _compile_workflow(
        tmp_path,
        workflow,
        setup_config(cache_root="/tmp/orchestrator-cache-b"),
        source_snapshot,
    )

    assert first.diagnostics == []
    assert second.diagnostics == []
    assert first.workflow_signature is not None
    assert first.workflow_signature == second.workflow_signature
    assert first.effective_runtime_config_signature == (
        second.effective_runtime_config_signature
    )


def test_strict_identity_includes_cache_root_in_workflow_signature(
    tmp_path: Path,
) -> None:
    workflow = setup_workflow()
    source_snapshot = source_snapshot_for_commit("a" * 40)
    first = _compile_workflow(
        tmp_path,
        workflow,
        setup_config(
            cache_root="/tmp/orchestrator-cache-a",
            identity={"include_cache_root": True},
        ),
        source_snapshot,
    )
    second = _compile_workflow(
        tmp_path,
        workflow,
        setup_config(
            cache_root="/tmp/orchestrator-cache-b",
            identity={"include_cache_root": True},
        ),
        source_snapshot,
    )

    assert first.diagnostics == []
    assert second.diagnostics == []
    assert first.workflow_signature is not None
    assert first.workflow_signature != second.workflow_signature
    assert first.effective_runtime_config_signature != (
        second.effective_runtime_config_signature
    )


def test_strict_identity_signs_effective_cache_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", tmp_path.as_posix())
    payload = workspace_signature_payload(
        RuntimeWorkspaceSettingsSnapshot(
            enabled=True,
            cache_root="~/orchestrator-cache",
            identity={"include_cache_root": True},
        )
    )

    assert payload["cache_root"] == (tmp_path / "orchestrator-cache").as_posix()


def branch_export_workflow(
    create_branch: bool,
    branch_name: str | None = None,
) -> WorkflowPlan:
    declaration: dict[str, str | bool] = {
        "kind": "worktree",
        "create_branch": create_branch,
    }
    if branch_name is not None:
        declaration["branch_name"] = branch_name
    return WorkflowPlan(
        name="workspace branch export",
        worktrees={"primary": declaration},
        nodes=[
            WorkflowNode(
                id="implement",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[PromptSegment(role="shared", content="run")],
            )
        ],
    )


def setup_workflow() -> WorkflowPlan:
    return WorkflowPlan(
        name="workspace setup",
        worktrees={"primary": {"kind": "worktree", "setup_profile": "bootstrap"}},
        nodes=[
            WorkflowNode(
                id="implement",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[PromptSegment(role="shared", content="run")],
            )
        ],
    )


def project_root_workflow() -> WorkflowPlan:
    return WorkflowPlan(
        name="project root workflow",
        nodes=[
            WorkflowNode(
                id="implement",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[PromptSegment(role="shared", content="run")],
            )
        ],
    )


def source_snapshot_for_commit(commit: str) -> WorkspaceSourceSnapshot:
    return WorkspaceSourceSnapshot(
        worktree_contract=WorktreeContract(),
        run_base_commit=commit,
        source_tree="b" * 40,
        object_format="sha1",
        repository_id="c" * 64,
        git_version="git version 2.34.1",
        git_top_level="/repo",
        project_root_relative_path=".",
        active_git_dir="/repo/.git",
        common_git_dir="/repo/.git",
        clean_start="strict",
    )


def setup_config(
    setup_timeout_seconds: int = 30,
    setup_commands: list[list[str]] | None = None,
    cache_root: str | None = None,
    identity: dict[str, bool] | None = None,
) -> Config:
    workspace: dict[str, object] = {
        "enabled": True,
        "setup_timeout_seconds": setup_timeout_seconds,
        "setup_profiles": {
            "bootstrap": {"run": setup_commands or [["python", "-c", "print('setup')"]]}
        },
    }
    if cache_root is not None:
        workspace["cache_root"] = cache_root
    if identity is not None:
        workspace["identity"] = identity
    return _config(workspace)


def _config(workspace: dict[str, object]) -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["echo"])},
        settings=Settings(workspace=workspace),
    )


def _compile_workflow(
    root: Path,
    workflow: WorkflowPlan,
    config: Config,
    source_snapshot: WorkspaceSourceSnapshot | None = None,
):
    return compile_source_with_source_snapshot(
        root,
        PreflightWorkflowSource.from_workflow(workflow),
        source_snapshot,
        config,
    )


def compile_source_with_source_snapshot(
    root: Path,
    source: PreflightWorkflowSource,
    source_snapshot: WorkspaceSourceSnapshot | None,
    config: Config | None = None,
):
    resolved_config = config or _config({"enabled": True})
    runtime_snapshot = build_runtime_config_snapshot(
        config=resolved_config,
        console=Console(file=None),
        no_live=True,
    )
    return compile_preflight_preview(
        source=source,
        config=resolved_config,
        runtime_snapshot=runtime_snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=root,
            orchestrator_dir=root / ".orchestrator",
            fingerprint_key_policy="read_only",
            workspace_source_snapshot=source_snapshot,
        ),
    )

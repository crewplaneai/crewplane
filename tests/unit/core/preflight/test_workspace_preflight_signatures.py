from __future__ import annotations

import json
from pathlib import Path

from crewplane.core.config import Config
from crewplane.core.preflight import (
    PreflightWorkflowSource,
)
from crewplane.core.preflight.models import (
    WorkspaceSourceSnapshot,
)
from crewplane.core.preflight.runtime_config import (
    RuntimeWorkspaceSettingsSnapshot,
    workspace_signature_payload,
)
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.composition.models import WorkflowSourceRecord
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.core.workspace.policy import WorktreeContract
from tests.unit.core.preflight.workspace_preflight_signatures_support import (
    compile_signature_workflow,
    compile_source_with_source_snapshot,
    project_root_workflow,
    workspace_signature_config,
)


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
    disabled = compile_signature_workflow(
        tmp_path, workflow, workspace_signature_config({"enabled": False})
    )
    enabled = compile_signature_workflow(
        tmp_path, workflow, workspace_signature_config({"enabled": True})
    )

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
    strict = compile_signature_workflow(
        tmp_path,
        workflow,
        workspace_signature_config(
            {
                "enabled": True,
                "clean_start": "strict",
                "setup_timeout_seconds": 30,
                "setup_profiles": {"bootstrap": {"run": [["uv", "sync"]]}},
            }
        ),
    )
    tracked_only = compile_signature_workflow(
        tmp_path,
        workflow,
        workspace_signature_config(
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


def test_project_root_workflow_excludes_workspace_setup_secrets(
    tmp_path: Path,
) -> None:
    workflow = project_root_workflow()
    baseline = compile_signature_workflow(
        tmp_path, workflow, workspace_signature_config({"enabled": True})
    )
    unused_secret = compile_signature_workflow(
        tmp_path,
        workflow,
        workspace_signature_config(
            {
                "enabled": True,
                "setup_profiles": {
                    "bootstrap": {
                        "run": [["setup", "--api-key", "UNUSED_WORKSPACE_SECRET"]]
                    }
                },
            }
        ),
    )

    assert baseline.diagnostics == []
    assert unused_secret.diagnostics == []
    assert baseline.workflow_signature is not None
    assert baseline.workflow_signature == unused_secret.workflow_signature
    assert unused_secret.fingerprint_metadata["sensitive_values_required"] is False
    assert unused_secret.runtime_config_snapshot is not None
    assert unused_secret.runtime_config_snapshot.sensitive_config_paths == []


def test_selected_workspace_setup_secret_remains_sensitive(
    tmp_path: Path,
) -> None:
    profile_name = "bootstrap.v2"
    secret = "WORKSPACE_SECRET"
    preview = compile_signature_workflow(
        tmp_path,
        setup_workflow(profile_name),
        workspace_signature_config(
            {
                "enabled": True,
                "setup_profiles": {
                    profile_name: {"run": [["setup", "--api-key", secret]]}
                },
            }
        ),
        source_snapshot_for_commit("a" * 40),
    )

    assert preview.diagnostics == []
    assert preview.fingerprint_metadata["sensitive_values_required"] is True
    assert preview.runtime_config_snapshot is not None
    sensitive_path = "workspace.setup_profiles.bootstrap.v2.run.0.2"
    assert preview.runtime_config_snapshot.sensitive_config_paths == [sensitive_path]
    assert preview.secret_context.get(f"config:{sensitive_path}") == secret


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
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                ],
                worktree="none",
            )
        ],
    )
    strict = compile_signature_workflow(
        tmp_path,
        workflow,
        workspace_signature_config(
            {
                "enabled": True,
                "clean_start": "strict",
                "setup_timeout_seconds": 30,
                "setup_profiles": {"bootstrap": {"run": [["uv", "sync"]]}},
            }
        ),
    )
    tracked_only = compile_signature_workflow(
        tmp_path,
        workflow,
        workspace_signature_config(
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
    thirty_seconds = compile_signature_workflow(
        tmp_path,
        workflow,
        setup_config(setup_timeout_seconds=30),
        source_snapshot,
    )
    sixty_seconds = compile_signature_workflow(
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
    uv_sync = compile_signature_workflow(
        tmp_path,
        workflow,
        setup_config(setup_commands=[["uv", "sync"]]),
        source_snapshot,
    )
    pip_install = compile_signature_workflow(
        tmp_path,
        workflow,
        setup_config(setup_commands=[["python", "-m", "pip", "install", "-e", "."]]),
        source_snapshot,
    )

    assert uv_sync.diagnostics == []
    assert pip_install.diagnostics == []
    assert uv_sync.workflow_signature is not None
    assert uv_sync.workflow_signature != pip_install.workflow_signature


def test_selected_setup_command_secrets_are_absent_from_compiled_plan(
    tmp_path: Path,
) -> None:
    secret = "WORKSPACE_SETUP_TEST_SECRET"
    preview = compile_signature_workflow(
        tmp_path,
        setup_workflow(),
        setup_config(setup_commands=[["provider", "--api-key", secret]]),
        source_snapshot_for_commit("a" * 40),
    )

    assert preview.diagnostics == []
    policy = preview.nodes[0].workspace_policy
    assert policy is not None
    assert policy.setup is not None
    redacted_token = policy.setup.commands[0].argv[2]
    assert isinstance(redacted_token, dict)
    assert redacted_token["redacted"] is True
    handle = redacted_token["value_handle"]
    assert isinstance(handle, str)
    assert preview.secret_context.get(handle) == secret
    assert secret not in json.dumps(
        preview.model_dump(mode="json"),
        sort_keys=True,
    )


def test_cache_root_does_not_change_default_workflow_signature(
    tmp_path: Path,
) -> None:
    workflow = setup_workflow()
    source_snapshot = source_snapshot_for_commit("a" * 40)
    first = compile_signature_workflow(
        tmp_path,
        workflow,
        setup_config(cache_root="/tmp/crewplane-cache-a"),
        source_snapshot,
    )
    second = compile_signature_workflow(
        tmp_path,
        workflow,
        setup_config(cache_root="/tmp/crewplane-cache-b"),
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
    first = compile_signature_workflow(
        tmp_path,
        workflow,
        setup_config(
            cache_root="/tmp/crewplane-cache-a",
            identity={"include_cache_root": True},
        ),
        source_snapshot,
    )
    second = compile_signature_workflow(
        tmp_path,
        workflow,
        setup_config(
            cache_root="/tmp/crewplane-cache-b",
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
            cache_root="~/crewplane-cache",
            identity={"include_cache_root": True},
        )
    )

    assert payload["cache_root"] == (tmp_path / "crewplane-cache").as_posix()


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
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                ],
            )
        ],
    )


def setup_workflow(setup_profile: str = "bootstrap") -> WorkflowPlan:
    return WorkflowPlan(
        name="workspace setup",
        worktrees={"primary": {"kind": "worktree", "setup_profile": setup_profile}},
        nodes=[
            WorkflowNode(
                id="implement",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                ],
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
    return workspace_signature_config(workspace)

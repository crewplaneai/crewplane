from __future__ import annotations

from pathlib import Path

from rich.console import Console

from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.preflight import (
    PreflightCompileOptions,
    PreflightWorkflowSource,
    compile_preflight_preview,
    load_workflow_source_for_preflight,
)
from crewplane.core.preflight.models import (
    PreflightCompilationPreview,
    WorkspaceSourceSnapshot,
)
from crewplane.core.preflight.plan_signatures import semantic_node_payloads
from crewplane.core.preflight.runtime_config import (
    RuntimeWorkspaceSettingsSnapshot,
    workspace_signature_payload,
)
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.composition.models import WorkflowSourceRecord
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.core.workspace.policy import WorktreeContract
from crewplane.version import SCHEMA_VERSION


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


def test_review_starts_with_changes_workflow_signature(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("review context\n", encoding="utf-8")
    config = review_loop_config()
    omitted = _compile_workflow(tmp_path, review_loop_workflow(), config)
    explicit_executor = _compile_workflow(
        tmp_path,
        review_loop_workflow("executor"),
        config,
    )
    reviewer_first = _compile_workflow(
        tmp_path,
        review_loop_workflow("reviewer"),
        config,
    )

    assert omitted.diagnostics == []
    assert explicit_executor.diagnostics == []
    assert reviewer_first.diagnostics == []
    assert omitted.workflow_signature is not None
    assert omitted.workflow_signature == explicit_executor.workflow_signature
    assert omitted.workflow_signature != reviewer_first.workflow_signature
    assert "review_starts_with" not in semantic_execution_policy(omitted)
    assert "review_starts_with" not in semantic_execution_policy(explicit_executor)
    assert semantic_execution_policy(reviewer_first)["review_starts_with"] == "reviewer"


def test_explicit_executor_default_in_markdown_keeps_workflow_signature(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("review context\n", encoding="utf-8")
    workflow_path = tmp_path / "review.task.md"
    config = review_loop_config()
    workflow_path.write_text(
        markdown_review_loop_workflow(review_starts_with=None),
        encoding="utf-8",
    )
    omitted = compile_source_with_source_snapshot(
        tmp_path,
        load_workflow_source_for_preflight(workflow_path, tmp_path),
        None,
        config,
    )
    workflow_path.write_text(
        markdown_review_loop_workflow(review_starts_with="executor"),
        encoding="utf-8",
    )
    explicit_executor = compile_source_with_source_snapshot(
        tmp_path,
        load_workflow_source_for_preflight(workflow_path, tmp_path),
        None,
        config,
    )

    assert omitted.diagnostics == []
    assert explicit_executor.diagnostics == []
    assert omitted.workflow_signature is not None
    assert omitted.workflow_signature == explicit_executor.workflow_signature


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
        setup_config(cache_root="/tmp/crewplane-cache-a"),
        source_snapshot,
    )
    second = _compile_workflow(
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
    first = _compile_workflow(
        tmp_path,
        workflow,
        setup_config(
            cache_root="/tmp/crewplane-cache-a",
            identity={"include_cache_root": True},
        ),
        source_snapshot,
    )
    second = _compile_workflow(
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


def setup_workflow() -> WorkflowPlan:
    return WorkflowPlan(
        name="workspace setup",
        worktrees={"primary": {"kind": "worktree", "setup_profile": "bootstrap"}},
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


def project_root_workflow() -> WorkflowPlan:
    return WorkflowPlan(
        name="project root workflow",
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


def review_loop_workflow(review_starts_with: str | None = None) -> WorkflowPlan:
    node_payload: dict[str, object] = {
        "id": "review.loop",
        "mode": "sequential",
        "providers": [
            ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR),
            ProviderSpec(provider="beta", role=ProviderRole.REVIEWER),
        ],
        "prompt_segments": [
            PromptSegment(role=PromptSegmentRole.SHARED, content="Review the change."),
            PromptSegment(
                role=PromptSegmentRole.REVIEWER,
                content="Inspect {{file:README.md}}.",
            ),
        ],
    }
    if review_starts_with is not None:
        node_payload["review_starts_with"] = review_starts_with
    return WorkflowPlan(
        name="review loop workflow",
        nodes=[WorkflowNode.model_validate(node_payload)],
    )


def markdown_review_loop_workflow(review_starts_with: str | None) -> str:
    review_starts_with_line = (
        f"    review_starts_with: {review_starts_with}\n"
        if review_starts_with is not None
        else ""
    )
    return (
        "---\n"
        f'schema_version: "{SCHEMA_VERSION}"\n'
        "name: review loop workflow\n"
        "nodes:\n"
        "  - id: review.loop\n"
        "    mode: sequential\n"
        f"{review_starts_with_line}"
        "    providers:\n"
        "      - provider: alpha\n"
        "        role: executor\n"
        "      - provider: beta\n"
        "        role: reviewer\n"
        "---\n"
        "\n"
        "## review.loop\n"
        "\n"
        "Review the change.\n"
        "\n"
        "<!-- crewplane:reviewer -->\n"
        "Inspect {{file:README.md}}.\n"
        "<!-- /crewplane:reviewer -->\n"
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


def review_loop_config() -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={
            "alpha": AgentConfig(cli_cmd=["echo"]),
            "beta": AgentConfig(cli_cmd=["echo"]),
        },
        settings=Settings(workspace={"enabled": False}),
    )


def semantic_execution_policy(
    preview: PreflightCompilationPreview,
) -> dict[str, object]:
    node_payload = semantic_node_payloads(preview.nodes)[0]
    assert isinstance(node_payload, dict)
    execution_policy = node_payload.get("execution_policy")
    assert isinstance(execution_policy, dict)
    return execution_policy


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
            state_dir=root / ".crewplane",
            fingerprint_key_policy="read_only",
            workspace_source_snapshot=source_snapshot,
        ),
    )

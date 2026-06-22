import pytest
from pydantic import ValidationError

from orchestrator_cli.core.config import AgentConfig, Config, Settings
from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.core.workflow_validation import (
    collect_workflow_policy_diagnostics,
    collect_workflow_validation_diagnostics,
)
from orchestrator_cli.core.workflow_validation_workspace import (
    logical_workspace_selections,
)
from orchestrator_cli.version import SCHEMA_VERSION


def _config(workspace_enabled: bool) -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={
            "alpha": AgentConfig(cli_cmd=["echo"]),
            "beta": AgentConfig(cli_cmd=["echo"]),
        },
        settings=Settings(workspace={"enabled": workspace_enabled}),
    )


def _executor_node(
    node_id: str,
    needs: list[str] | None = None,
    worktree: str | None = None,
    providers: list[ProviderSpec] | None = None,
) -> WorkflowNode:
    return WorkflowNode(
        id=node_id,
        mode="sequential",
        needs=needs or [],
        providers=providers or [ProviderSpec(provider="alpha")],
        prompt_segments=[PromptSegment(role="shared", content=f"run {node_id}")],
        worktree=worktree,
    )


def _messages(workflow: WorkflowPlan, config: Config) -> tuple[str, ...]:
    return tuple(
        diagnostic.message
        for diagnostic in collect_workflow_policy_diagnostics(workflow, config)
    )


def test_removed_workflow_workspace_block_is_rejected() -> None:
    with pytest.raises(
        ValidationError,
        match="workflow workspace blocks have been removed",
    ):
        WorkflowPlan.model_validate(
            {
                "name": "old workspace",
                "workspace": {"strategy": "snapshot", "from": "project"},
                "nodes": [_executor_node("implement").model_dump(mode="json")],
            }
        )


def test_removed_node_workspace_block_is_rejected() -> None:
    with pytest.raises(
        ValidationError,
        match="node workspace blocks have been removed",
    ):
        WorkflowNode.model_validate(
            {
                "id": "implement",
                "mode": "sequential",
                "providers": ["alpha"],
                "workspace": {"strategy": "git_worktree", "from": "project"},
            }
        )


def test_worktrees_are_rejected_when_workspace_is_disabled() -> None:
    workflow = WorkflowPlan(
        name="disabled workspace",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[_executor_node("implement")],
    )

    messages = _messages(workflow, _config(workspace_enabled=False))

    assert len(messages) == 1
    assert "settings.workspace.enabled: true" in messages[0]


def test_workflow_without_worktrees_uses_project_root_execution() -> None:
    workflow = WorkflowPlan(
        name="enabled defaults",
        nodes=[_executor_node("inspect")],
    )

    messages = _messages(workflow, _config(workspace_enabled=True))
    selections = logical_workspace_selections(workflow, _config(workspace_enabled=True))

    assert messages == ()
    assert selections["inspect"].enabled is False
    assert selections["inspect"].materialization == "project_root"


def test_single_worktree_is_inherited_and_sources_from_direct_upstream() -> None:
    workflow = WorkflowPlan(
        name="lineage",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            _executor_node("implement"),
            _executor_node("fix", needs=["implement"]),
        ],
    )

    selections = logical_workspace_selections(workflow, _config(workspace_enabled=True))

    assert _messages(workflow, _config(workspace_enabled=True)) == ()
    assert selections["implement"].logical_worktree_name == "primary"
    assert selections["fix"].source_kind == "node"
    assert selections["fix"].source_node_id == "implement"


def test_single_worktree_explicit_selector_emits_redundant_warning() -> None:
    workflow = WorkflowPlan(
        name="redundant selector",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[_executor_node("implement", worktree="primary")],
    )

    diagnostics = collect_workflow_policy_diagnostics(
        workflow,
        _config(workspace_enabled=True),
    )

    assert len(diagnostics) == 1
    assert diagnostics[0].severity == "warning"
    assert "selector is redundant" in diagnostics[0].message


def test_same_worktree_parallel_roots_are_rejected() -> None:
    workflow = WorkflowPlan(
        name="forked lineage",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            _executor_node("left"),
            _executor_node("right"),
        ],
    )

    messages = _messages(workflow, _config(workspace_enabled=True))

    assert len(messages) == 1
    assert (
        "Nodes 'left' and 'right' both select logical worktree 'primary'" in messages[0]
    )
    assert "add a needs edge" in messages[0]
    assert "use separate worktrees" in messages[0]


def test_same_worktree_many_parallel_roots_emit_one_diagnostic() -> None:
    workflow = WorkflowPlan(
        name="wide forked lineage",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            _executor_node("left"),
            _executor_node("middle"),
            _executor_node("right"),
        ],
    )

    diagnostics = collect_workflow_policy_diagnostics(
        workflow,
        _config(workspace_enabled=True),
    )

    assert len(diagnostics) == 1
    assert diagnostics[0].metadata == {
        "left_node": "left",
        "right_node": "middle",
        "worktree": "primary",
    }


def test_same_worktree_parallel_branches_emit_one_diagnostic() -> None:
    workflow = WorkflowPlan(
        name="branched lineage",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            _executor_node("prepare"),
            _executor_node("left", needs=["prepare"]),
            _executor_node("right", needs=["prepare"]),
        ],
    )

    diagnostics = collect_workflow_policy_diagnostics(
        workflow,
        _config(workspace_enabled=True),
    )

    assert len(diagnostics) == 1
    assert diagnostics[0].metadata == {
        "left_node": "left",
        "right_node": "right",
        "worktree": "primary",
    }


def test_same_worktree_ordered_direct_parents_select_latest_source() -> None:
    workflow = WorkflowPlan(
        name="ordered lineage",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            _executor_node("prepare"),
            _executor_node("implement", needs=["prepare"]),
            _executor_node("verify", needs=["prepare", "implement"]),
        ],
    )

    selections = logical_workspace_selections(workflow, _config(workspace_enabled=True))

    assert _messages(workflow, _config(workspace_enabled=True)) == ()
    assert selections["verify"].source_kind == "node"
    assert selections["verify"].source_node_id == "implement"


def test_same_worktree_after_project_root_node_uses_transitive_lineage() -> None:
    workflow = WorkflowPlan(
        name="transitive lineage",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            _executor_node("implement"),
            _executor_node("inspect", needs=["implement"], worktree="none"),
            _executor_node("fix", needs=["inspect"]),
        ],
    )

    selections = logical_workspace_selections(workflow, _config(workspace_enabled=True))

    assert _messages(workflow, _config(workspace_enabled=True)) == ()
    assert selections["fix"].source_kind == "node"
    assert selections["fix"].source_node_id == "implement"


def test_workspace_policy_diagnostics_do_not_raise_for_invalid_graph() -> None:
    workflow = WorkflowPlan(
        name="invalid graph",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[_executor_node("implement", needs=["missing"])],
    )

    diagnostics = collect_workflow_policy_diagnostics(
        workflow,
        _config(workspace_enabled=True),
    )
    reference_messages = tuple(
        diagnostic.message
        for diagnostic in collect_workflow_validation_diagnostics(workflow)
    )

    assert diagnostics == ()
    assert "Node 'implement' depends on unknown node 'missing'." in reference_messages


def test_multiple_worktrees_require_selectors_or_project_root_opt_out() -> None:
    workflow = WorkflowPlan(
        name="multi worktree",
        worktrees={
            "left": {"kind": "worktree"},
            "right": {"kind": "snapshot"},
        },
        nodes=[_executor_node("implement")],
    )

    messages = _messages(workflow, _config(workspace_enabled=True))

    assert len(messages) == 1
    assert "must select a worktree or set worktree: none" in messages[0]


def test_worktree_none_opts_out_to_project_root() -> None:
    workflow = WorkflowPlan(
        name="project root opt out",
        worktrees={
            "left": {"kind": "worktree"},
            "right": {"kind": "snapshot"},
        },
        nodes=[_executor_node("inspect", worktree="none")],
    )

    selections = logical_workspace_selections(workflow, _config(workspace_enabled=True))

    assert _messages(workflow, _config(workspace_enabled=True)) == ()
    assert selections["inspect"].enabled is False
    assert selections["inspect"].materialization == "project_root"


def test_unknown_worktree_selector_is_rejected() -> None:
    with pytest.raises(
        ValidationError,
        match="selects unknown worktree",
    ):
        WorkflowPlan(
            name="unknown worktree",
            worktrees={"primary": {"kind": "worktree"}},
            nodes=[_executor_node("implement", worktree="other")],
        )


def test_cross_worktree_dependency_is_rejected() -> None:
    workflow = WorkflowPlan(
        name="cross worktree",
        worktrees={
            "left": {"kind": "worktree"},
            "right": {"kind": "worktree"},
        },
        nodes=[
            _executor_node("implement", worktree="left"),
            _executor_node("fix", needs=["implement"], worktree="right"),
        ],
    )

    messages = _messages(workflow, _config(workspace_enabled=True))

    assert len(messages) == 1
    assert "Automatic merge between logical worktrees is not supported" in messages[0]


def test_mutable_workspace_rejects_multiple_executor_providers() -> None:
    workflow = WorkflowPlan(
        name="multi executor mutable",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            _executor_node(
                "implement",
                providers=[
                    ProviderSpec(provider="alpha", role="executor"),
                    ProviderSpec(provider="beta", role="executor"),
                ],
            )
        ],
    )

    messages = _messages(workflow, _config(workspace_enabled=True))

    assert len(messages) == 1
    assert "multiple executor providers" in messages[0]


def test_snapshot_worktree_rejects_worktree_only_fields() -> None:
    for field, value in (
        ("setup_profile", "bootstrap"),
        ("create_branch", True),
        ("branch_name", "ai/example"),
    ):
        with pytest.raises(ValidationError, match=field):
            WorkflowPlan(
                name=f"invalid snapshot {field}",
                worktrees={"scratch": {"kind": "snapshot", field: value}},
                nodes=[_executor_node("review")],
            )


@pytest.mark.parametrize(
    "branch_name",
    [
        "",
        " ",
        " ai/example",
        "ai/example ",
        "refs/heads/ai/example",
        "ai bad",
        "ai..bad",
        "ai/bad.lock",
        ".hidden",
        "HEAD",
        "-bad",
    ],
)
def test_worktree_branch_name_validates_raw_value_and_git_ref_syntax(
    branch_name: str,
) -> None:
    with pytest.raises(ValidationError, match="branch_name"):
        WorkflowPlan(
            name="invalid branch export",
            worktrees={
                "primary": {
                    "kind": "worktree",
                    "create_branch": True,
                    "branch_name": branch_name,
                }
            },
            nodes=[_executor_node("implement")],
        )


@pytest.mark.parametrize("create_branch", ["true", "false", "yes", 1])
def test_worktree_create_branch_requires_boolean(create_branch: object) -> None:
    with pytest.raises(ValidationError):
        WorkflowPlan(
            name="invalid branch export flag",
            worktrees={
                "primary": {
                    "kind": "worktree",
                    "create_branch": create_branch,
                }
            },
            nodes=[_executor_node("implement")],
        )


def test_selected_branch_exports_require_distinct_branch_names() -> None:
    workflow = WorkflowPlan(
        name="duplicate branch export",
        worktrees={
            "left": {
                "kind": "worktree",
                "create_branch": True,
                "branch_name": "feature/generated",
            },
            "right": {
                "kind": "worktree",
                "create_branch": True,
                "branch_name": "feature/generated",
            },
        },
        nodes=[
            _executor_node("implement-left", worktree="left"),
            _executor_node("implement-right", worktree="right"),
        ],
    )

    messages = _messages(workflow, _config(workspace_enabled=True))

    assert len(messages) == 1
    assert "Selected worktrees 'left', 'right'" in messages[0]
    assert "feature/generated" in messages[0]
    assert "distinct branch names" in messages[0]


def test_selected_branch_exports_validate_generated_branch_name_collisions() -> None:
    workflow = WorkflowPlan(
        name="duplicate generated branch export",
        worktrees={
            "a.b": {
                "kind": "worktree",
                "create_branch": True,
            },
            "a..b": {
                "kind": "worktree",
                "create_branch": True,
            },
        },
        nodes=[
            _executor_node("implement-left", worktree="a.b"),
            _executor_node("implement-right", worktree="a..b"),
        ],
    )

    messages = _messages(workflow, _config(workspace_enabled=True))

    assert len(messages) == 1
    assert "Selected worktrees 'a..b', 'a.b'" in messages[0]
    assert "orchestrator/duplicate-generated-branch-export/a.b/run" in messages[0]
    assert "distinct branch names" in messages[0]


def test_workflow_worktrees_cannot_define_setup_command_bodies() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        WorkflowPlan(
            name="workflow-owned setup body",
            worktrees={
                "primary": {
                    "kind": "worktree",
                    "setup_profile": "bootstrap",
                    "run": [["uv", "sync"]],
                }
            },
            nodes=[_executor_node("implement")],
        )


def test_unknown_setup_profile_fails_policy_validation() -> None:
    workflow = WorkflowPlan(
        name="missing setup",
        worktrees={"primary": {"kind": "worktree", "setup_profile": "missing"}},
        nodes=[_executor_node("implement")],
    )

    messages = _messages(workflow, _config(workspace_enabled=True))

    assert len(messages) == 1
    assert "unknown setup_profile 'missing'" in messages[0]


def test_input_nodes_reject_explicit_worktree_selectors() -> None:
    with pytest.raises(
        ValidationError,
        match="input nodes cannot declare worktree selectors",
    ):
        WorkflowNode(
            id="requirements",
            mode="input",
            source="{{file:docs/requirements.md}}",
            worktree="none",
        )

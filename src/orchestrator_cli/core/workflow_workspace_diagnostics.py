from __future__ import annotations

from typing import Literal

from orchestrator_cli.core.config import Config, Settings
from orchestrator_cli.core.workflow_diagnostics import WorkflowValidationDiagnostic
from orchestrator_cli.core.workflow_graph import ancestor_map
from orchestrator_cli.core.workflow_models import WorkflowNode, WorkflowPlan
from orchestrator_cli.core.workspace_policy import generated_branch_name
from orchestrator_cli.core.workspace_selection import LogicalWorkspaceSelection

WORKFLOW_WORKSPACE_CODE = "WORKFLOW-WORKSPACE"
WORKSPACE_POLICY_PHASE = "workspace_policy"
DISABLED_WORKSPACE_MESSAGE = (
    "Managed worktrees require settings.workspace.enabled: true. Enable workspace "
    "isolation or remove workflow worktrees."
)


def workspace_policy_diagnostics(
    workflow: WorkflowPlan,
    config: Config,
    selections: dict[str, LogicalWorkspaceSelection],
) -> tuple[WorkflowValidationDiagnostic, ...]:
    settings = config.settings if config.settings is not None else Settings()
    diagnostics: list[WorkflowValidationDiagnostic] = []
    if workflow.worktrees and not settings.workspace.enabled:
        diagnostics.append(_diagnostic(DISABLED_WORKSPACE_MESSAGE))

    selected_worktrees = {
        selection.logical_worktree_name
        for selection in selections.values()
        if selection.logical_worktree_name is not None
    }
    diagnostics.extend(
        _unselected_branch_export_diagnostics(workflow, selected_worktrees)
    )
    if settings.workspace.enabled:
        diagnostics.extend(
            _duplicate_branch_export_diagnostics(workflow, selected_worktrees)
        )
    diagnostics.extend(_missing_selector_diagnostics(workflow))
    diagnostics.extend(_setup_profile_diagnostics(config, selections))
    diagnostics.extend(_mutable_executor_diagnostics(workflow, selections))
    diagnostics.extend(_unordered_writer_diagnostics(workflow, selections))
    diagnostics.extend(_source_lineage_diagnostics(workflow, selections))
    diagnostics.extend(_redundant_selector_diagnostics(workflow))
    return tuple(diagnostics)


def _duplicate_branch_export_diagnostics(
    workflow: WorkflowPlan,
    selected_worktrees: set[str | None],
) -> tuple[WorkflowValidationDiagnostic, ...]:
    worktrees_by_branch: dict[str, list[str]] = {}
    for name, declaration in workflow.worktrees.items():
        if (
            name not in selected_worktrees
            or declaration.kind != "worktree"
            or not declaration.create_branch
        ):
            continue
        branch_name = declaration.branch_name or generated_branch_name(
            workflow.name,
            name,
            "run",
        )
        worktrees_by_branch.setdefault(branch_name, []).append(name)

    diagnostics: list[WorkflowValidationDiagnostic] = []
    for branch_name, worktree_names in sorted(worktrees_by_branch.items()):
        if len(worktree_names) <= 1:
            continue
        joined_worktrees = ", ".join(f"'{name}'" for name in sorted(worktree_names))
        diagnostics.append(
            _diagnostic(
                "Selected worktrees "
                f"{joined_worktrees} request branch export to the same branch "
                f"'{branch_name}'. Branch-exporting worktrees must target distinct "
                "branch names.",
                metadata={
                    "branch_name": branch_name,
                    "worktrees": ", ".join(sorted(worktree_names)),
                },
            )
        )
    return tuple(diagnostics)


def _unselected_branch_export_diagnostics(
    workflow: WorkflowPlan,
    selected_worktrees: set[str | None],
) -> tuple[WorkflowValidationDiagnostic, ...]:
    diagnostics: list[WorkflowValidationDiagnostic] = []
    for name, declaration in workflow.worktrees.items():
        if (
            declaration.kind == "worktree"
            and declaration.create_branch
            and name not in selected_worktrees
        ):
            diagnostics.append(
                _diagnostic(
                    f"Worktree '{name}' requests branch export but no provider node "
                    "selects it."
                )
            )
    return tuple(diagnostics)


def _missing_selector_diagnostics(
    workflow: WorkflowPlan,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    if len(workflow.worktrees) <= 1:
        return ()
    return tuple(
        _diagnostic(
            f"Node '{node.id}' must select a worktree or set worktree: none "
            "because the workflow declares multiple worktrees.",
            node.id,
        )
        for node in workflow.nodes
        if node.mode != "input" and node.worktree is None
    )


def _setup_profile_diagnostics(
    config: Config,
    selections: dict[str, LogicalWorkspaceSelection],
) -> tuple[WorkflowValidationDiagnostic, ...]:
    settings = config.settings if config.settings is not None else Settings()
    diagnostics: list[WorkflowValidationDiagnostic] = []
    for selection in selections.values():
        if (
            selection.setup_profile is not None
            and selection.setup_profile not in settings.workspace.setup_profiles
        ):
            diagnostics.append(
                _diagnostic(
                    f"Node '{selection.node_id}' selects worktree "
                    f"'{selection.logical_worktree_name}' with unknown setup_profile "
                    f"'{selection.setup_profile}'.",
                    selection.node_id,
                )
            )
    return tuple(diagnostics)


def _mutable_executor_diagnostics(
    workflow: WorkflowPlan,
    selections: dict[str, LogicalWorkspaceSelection],
) -> tuple[WorkflowValidationDiagnostic, ...]:
    diagnostics: list[WorkflowValidationDiagnostic] = []
    nodes_by_id = {node.id: node for node in workflow.nodes}
    for selection in selections.values():
        if selection.declaration_kind != "worktree":
            continue
        node = nodes_by_id[selection.node_id]
        executor_count = sum(
            1 for provider in node.providers if provider.role == "executor"
        )
        if executor_count > 1:
            diagnostics.append(
                _diagnostic(
                    f"Node '{node.id}' selects mutable worktree "
                    f"'{selection.logical_worktree_name}' but defines multiple "
                    "executor providers. Worktree lineage allows one mutable "
                    "executor; reviewers remain parallel-safe.",
                    node.id,
                    {
                        "executor_count": executor_count,
                        "worktree": selection.logical_worktree_name,
                    },
                )
            )
    return tuple(diagnostics)


def _source_lineage_diagnostics(
    workflow: WorkflowPlan,
    selections: dict[str, LogicalWorkspaceSelection],
) -> tuple[WorkflowValidationDiagnostic, ...]:
    diagnostics: list[WorkflowValidationDiagnostic] = []
    for node in workflow.nodes:
        selection = selections.get(node.id)
        if selection is None or selection.declaration_kind != "worktree":
            continue
        diagnostics.extend(
            _cross_worktree_dependency_diagnostics(node, selection, selections)
        )
    return tuple(diagnostics)


def _unordered_writer_diagnostics(
    workflow: WorkflowPlan,
    selections: dict[str, LogicalWorkspaceSelection],
) -> tuple[WorkflowValidationDiagnostic, ...]:
    ancestors = ancestor_map(workflow)
    node_order = {node.id: index for index, node in enumerate(workflow.nodes)}
    diagnostics: list[WorkflowValidationDiagnostic] = []
    for worktree_name, node_ids in _lineage_writers_by_worktree(selections).items():
        unordered_pair = _first_unordered_writer_pair(node_ids, ancestors, node_order)
        if unordered_pair is None:
            continue
        left, right = unordered_pair
        diagnostics.append(_unordered_writer_diagnostic(worktree_name, left, right))
    return tuple(diagnostics)


def _lineage_writers_by_worktree(
    selections: dict[str, LogicalWorkspaceSelection],
) -> dict[str, list[str]]:
    writers_by_worktree: dict[str, list[str]] = {}
    for selection in selections.values():
        if selection.lineage_producer and selection.logical_worktree_name is not None:
            writers_by_worktree.setdefault(selection.logical_worktree_name, []).append(
                selection.node_id
            )
    return writers_by_worktree


def _first_unordered_writer_pair(
    node_ids: list[str],
    ancestors: dict[str, set[str]],
    node_order: dict[str, int],
) -> tuple[str, str] | None:
    ordered_node_ids = sorted(
        node_ids,
        key=lambda node_id: (
            len(ancestors[node_id]),
            node_order[node_id],
        ),
    )
    for left, right in zip(ordered_node_ids, ordered_node_ids[1:], strict=False):
        if left not in ancestors[right]:
            return left, right
    return None


def _unordered_writer_diagnostic(
    worktree_name: str,
    left: str,
    right: str,
) -> WorkflowValidationDiagnostic:
    return _diagnostic(
        f"Nodes '{left}' and '{right}' both select logical worktree "
        f"'{worktree_name}' but they are not ordered by the DAG. "
        "One mutable source line cannot have unordered writers; "
        "add a needs edge to serialize the source line or use "
        "separate worktrees.",
        right,
        {
            "left_node": left,
            "right_node": right,
            "worktree": worktree_name,
        },
    )


def _cross_worktree_dependency_diagnostics(
    node: WorkflowNode,
    selection: LogicalWorkspaceSelection,
    selections: dict[str, LogicalWorkspaceSelection],
) -> tuple[WorkflowValidationDiagnostic, ...]:
    diagnostics: list[WorkflowValidationDiagnostic] = []
    for dependency in node.needs:
        upstream = selections.get(dependency)
        if (
            upstream is not None
            and upstream.lineage_producer
            and upstream.logical_worktree_name != selection.logical_worktree_name
        ):
            diagnostics.append(
                _diagnostic(
                    f"Node '{node.id}' depends directly on worktree "
                    f"'{upstream.logical_worktree_name}' while selecting worktree "
                    f"'{selection.logical_worktree_name}'. Automatic merge between "
                    "logical worktrees is not supported; pass artifacts or select "
                    "worktree: none.",
                    node.id,
                    {
                        "dependency": dependency,
                        "source_worktree": upstream.logical_worktree_name,
                        "target_worktree": selection.logical_worktree_name,
                    },
                )
            )
    return tuple(diagnostics)


def _redundant_selector_diagnostics(
    workflow: WorkflowPlan,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    if len(workflow.worktrees) != 1:
        return ()
    only_name = next(iter(workflow.worktrees))
    return tuple(
        _diagnostic(
            f"Node '{node.id}' explicitly selects the only declared worktree "
            f"'{only_name}'; the selector is redundant.",
            node.id,
            severity="warning",
        )
        for node in workflow.nodes
        if node.mode != "input" and node.worktree == only_name
    )


def _diagnostic(
    message: str,
    node_id: str | None = None,
    metadata: dict[str, str | int | bool | None] | None = None,
    severity: Literal["error", "warning"] = "error",
) -> WorkflowValidationDiagnostic:
    return WorkflowValidationDiagnostic(
        code=WORKFLOW_WORKSPACE_CODE,
        phase=WORKSPACE_POLICY_PHASE,
        message=message,
        severity=severity,
        node_id=node_id,
        metadata=metadata or {},
    )

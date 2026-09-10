from pathlib import Path

from crewplane.architecture.contracts import (
    InvocationContext,
    InvocationSourceContext,
    InvocationWorkspaceContext,
    InvocationWorktreeContract,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.version import SCHEMA_VERSION


def command_workspace_context(
    cwd: Path,
    recorder,
) -> InvocationContext:
    return InvocationContext(
        node_id="node.a",
        task_id="generic_executor_0",
        provider="generic",
        role=ProviderRole.EXECUTOR,
        workspace_environment_applied_recorder=recorder,
        workspace=InvocationWorkspaceContext(
            workspace_kind="snapshot",
            materialization="snapshot_checkout",
            logical_worktree_name="primary",
            cwd=cwd,
            invocation_source=InvocationSourceContext(
                source_kind="project",
                source_node_id=None,
                source_commit="a" * 40,
                source_tree="b" * 40,
            ),
            worktree_contract=InvocationWorktreeContract(
                mode="blob_exact",
                schema_version=SCHEMA_VERSION,
            ),
            child_environment_required=True,
            child_environment_applied=False,
        ),
    )

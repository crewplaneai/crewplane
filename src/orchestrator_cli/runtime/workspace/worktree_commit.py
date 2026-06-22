from __future__ import annotations

import json
from pathlib import Path

from orchestrator_cli.core.workspace_git_policy import (
    deterministic_workspace_commit_environment,
)

from .git import GitCommand
from .worktree_types import WorktreeCaptureRequest


def commit_tree(
    checkout_root: Path,
    tree: str,
    parent: str,
    message: str,
) -> str:
    result = GitCommand(
        cwd=checkout_root,
        env=deterministic_workspace_commit_environment(),
    ).run_with_input(
        message.encode("utf-8"),
        "commit-tree",
        tree,
        "-p",
        parent,
    )
    return result.stdout.decode("utf-8").strip()


def commit_message(request: WorktreeCaptureRequest, tree: str) -> str:
    payload = {
        "worktree_contract": request.plan.workspace_source.worktree_contract.model_dump(
            mode="json"
        )
        if request.plan.workspace_source is not None
        else None,
        "node_id": request.node_id,
        "parent": request.source_ref.source_commit,
        "source_kind": request.source_ref.source_kind,
        "source_node_id": request.source_ref.source_node_id,
        "task_id": request.task_id,
        "tree": tree,
        "workflow_signature": request.plan.workflow_signature,
    }
    return "orchestrator-cli workspace result\n\n" + json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    )

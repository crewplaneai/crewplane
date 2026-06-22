from __future__ import annotations

import json
from pathlib import Path

from orchestrator_cli.architecture.contracts import (
    InvocationContext,
    JsonObject,
    MockInvokerOptions,
)

from .outputs import OutputResolution


def write_invocation_log(
    options: MockInvokerOptions,
    log_file: Path | None,
    output_file: Path,
    cwd: Path,
    context: InvocationContext | None,
    resolution: OutputResolution,
) -> None:
    if log_file is None:
        return
    log_file.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "invoker": "mock",
        "output_mode": options.output_mode,
        "source": resolution.source,
        "fixture_path": (
            str(resolution.fixture_path)
            if resolution.fixture_path is not None
            else None
        ),
        "cwd": str(cwd),
        "output_file": str(output_file),
        "node_id": context.node_id if context is not None else None,
        "task_id": context.task_id if context is not None else None,
        "provider": context.provider if context is not None else None,
        "role": context.role if context is not None else None,
        "audit_round_num": context.audit_round_num if context is not None else None,
        "round_num": context.round_num if context is not None else None,
        "workspace": _workspace_record(context),
    }
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _workspace_record(context: InvocationContext | None) -> JsonObject | None:
    if context is None or context.workspace is None:
        return None
    workspace = context.workspace
    return {
        "candidate_commit": workspace.candidate_commit,
        "child_environment_applied": workspace.child_environment_applied,
        "child_environment_required": workspace.child_environment_required,
        "cwd": str(workspace.cwd),
        "lineage_producer": workspace.lineage_producer,
        "logical_worktree_name": workspace.logical_worktree_name,
        "materialization": workspace.materialization,
        "result_commit": workspace.result_commit,
        "workspace_kind": workspace.workspace_kind,
        "worktree_contract": {
            "mode": workspace.worktree_contract.mode,
            "schema_version": workspace.worktree_contract.schema_version,
        },
        "workspace_state_path": (
            str(workspace.workspace_state_path)
            if workspace.workspace_state_path is not None
            else None
        ),
        "writable": workspace.writable,
        "invocation_source": {
            "candidate_sequence": workspace.invocation_source.candidate_sequence,
            "source_commit": workspace.invocation_source.source_commit,
            "source_kind": workspace.invocation_source.source_kind,
            "source_node_id": workspace.invocation_source.source_node_id,
            "source_tree": workspace.invocation_source.source_tree,
        },
    }

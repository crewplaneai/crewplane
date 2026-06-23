from __future__ import annotations

import hashlib
import re
from pathlib import Path

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
)

MAX_INVOCATION_SLUG_CHARS = 160
INVOCATION_SLUG_HASH_CHARS = 12


def node_by_id(
    plan: PreflightExecutionPlan,
    node_id: str,
) -> PreflightExecutionNode:
    for node in plan.nodes:
        if node.id == node_id:
            return node
    raise ValueError(f"Compiled plan does not contain node '{node_id}'.")


def workspace_state_path(
    output: ArtifactStorePort,
    node: PreflightExecutionNode,
    slug: str,
    audit_round_num: int | None,
    round_num: int = 1,
) -> Path:
    stage_dir = output.get_stage_dir(node.id)
    if stage_dir is None:
        stage_dir = output.create_stage_dir(node.id)
    if len(node.provider_records) == 1 and audit_round_num is None and round_num == 1:
        return stage_dir / "workspace-state.json"
    return stage_dir / f"workspace-state-{slug}.json"


def invocation_slug(
    node_id: str,
    task_id: str,
    audit_round_num: int | None,
    round_num: int,
) -> str:
    audit = f"audit{audit_round_num}-" if audit_round_num is not None else ""
    raw_slug = f"{node_id}-{task_id}-{audit}round{round_num}"
    return bounded_invocation_slug(raw_slug)


def bounded_invocation_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if not slug:
        return _short_slug_hash(value)
    if len(slug) <= MAX_INVOCATION_SLUG_CHARS:
        return slug
    suffix = f"--{_short_slug_hash(value)}"
    available = MAX_INVOCATION_SLUG_CHARS - len(suffix)
    prefix = slug[:available].rstrip(".-")
    if not prefix:
        prefix = "workspace"[:available]
    return f"{prefix}{suffix}"


def _short_slug_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[
        :INVOCATION_SLUG_HASH_CHARS
    ]


def workspace_cleanup_on_success(plan: PreflightExecutionPlan) -> bool:
    workspace = plan.runtime_config_snapshot.get("workspace")
    if not isinstance(workspace, dict):
        return True
    value = workspace.get("cleanup_on_success")
    return value if isinstance(value, bool) else True


def controlled_child_environment_required(plan: PreflightExecutionPlan) -> bool:
    invoker = plan.runtime_config_snapshot.get("invoker")
    if not isinstance(invoker, dict):
        return False
    capabilities = invoker.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    workspace = capabilities.get("workspace")
    if not isinstance(workspace, dict):
        return False
    return (
        workspace.get("launch_mode") == "runtime_command_runner"
        and workspace.get("controlled_child_environment") is True
    )

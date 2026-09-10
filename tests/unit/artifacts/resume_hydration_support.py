from __future__ import annotations

import json
from pathlib import Path

from crewplane.artifacts.manager import OutputManager
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import (
    attach_workspace_descriptor,
    make_plan,
    make_run_manifest,
    make_workspace_source_snapshot,
)
from tests.helpers.resume_validation import snapshot_workspace_state_payload
from tests.helpers.workspace_records import workspace_selection_record


def hydration_output(tmp_path: Path) -> OutputManager:
    output = OutputManager("Workflow", base_dir=tmp_path)
    output.write_run_manifest(
        make_run_manifest(output.run_id, output.run_key_name, status="running")
    )
    return output


def workspace_snapshot_plan():
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="snapshot",
        clean_start="strict",
        materialization="snapshot_checkout",
    )
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    return plan.model_copy(
        update={
            "nodes": [node, plan.nodes[1]],
            "workspace_source": make_workspace_source_snapshot(),
            "runtime_config_snapshot": {
                "schema_version": SCHEMA_VERSION,
                "invoker": {
                    "implementation": "mock",
                    "capabilities": {
                        "workspace": {
                            "honors_cwd": True,
                            "launch_mode": "mock_no_child_process",
                            "controlled_child_environment": False,
                        }
                    },
                },
            },
        }
    )


def workspace_worktree_plan():
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    return plan.model_copy(
        update={
            "nodes": [node, plan.nodes[1]],
            "workspace_source": make_workspace_source_snapshot(),
            "runtime_config_snapshot": {
                "schema_version": SCHEMA_VERSION,
                "invoker": {
                    "implementation": "mock",
                    "capabilities": {
                        "workspace": {
                            "honors_cwd": True,
                            "launch_mode": "mock_no_child_process",
                            "controlled_child_environment": False,
                        }
                    },
                },
            },
        }
    )


def write_lineage_workspace_state(path, result_commit: str, round_num: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "status": "succeeded",
                "role": "executor",
                "task_id": "alpha",
                "round_num": round_num,
                "audit_round_num": None,
                "workspace": {"lineage_producer": True},
                "result": {
                    "result_commit": result_commit,
                    "result_tree": "b" * 40,
                },
            }
        ),
        encoding="utf-8",
    )


def write_snapshot_workspace_state(source, plan) -> None:
    payload = snapshot_workspace_state_payload(source, plan, "alpha")
    state_path = source.run_dir / "a" / "workspace-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    attach_workspace_descriptor(source.run_dir, plan, "a")

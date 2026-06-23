from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from crewplane.artifacts.manager import OutputManager
from crewplane.artifacts.workspace.node_state import (
    build_node_workspace_descriptor,
    refresh_node_workspace_descriptor,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import (
    WORKTREE_CONTRACT_PAYLOAD,
    make_node_state,
    make_plan,
    make_run_manifest,
    make_workspace_source_snapshot,
    write_result,
)
from tests.helpers.workspace_records import workspace_selection_record


def test_build_node_workspace_descriptor_records_state_and_bundle_artifacts(
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    plan = _workspace_plan()
    stage_dir = output.create_stage_dir("a")
    bundle_payload = b"bundle"
    bundle_path = stage_dir / "workspace-bundles" / "a.bundle"
    bundle_path.parent.mkdir(parents=True)
    bundle_path.write_bytes(bundle_payload)
    state_path = stage_dir / "workspace-state.json"
    state_path.write_text(
        json.dumps(_workspace_state_payload(plan, bundle_payload), sort_keys=True),
        encoding="utf-8",
    )

    descriptor = build_node_workspace_descriptor(plan.nodes[0], plan, output)

    assert descriptor is not None
    assert descriptor["enabled"] is True
    assert descriptor["worktree_contract"] == WORKTREE_CONTRACT_PAYLOAD
    assert descriptor["lineage_producer"] is True
    assert descriptor["invoker"]["launch_mode"] == "mock_no_child_process"
    states = descriptor["states"]
    assert isinstance(states, list)
    assert len(states) == 1
    state = states[0]
    assert isinstance(state, dict)
    artifact = state["workspace_state_artifact"]
    assert artifact["relative_path"] == "a/workspace-state.json"
    assert artifact["size_bytes"] == state_path.stat().st_size
    assert isinstance(artifact["resume_sha256"], str)
    assert isinstance(artifact["resume_size_bytes"], int)
    assert state["invocation_source"]["source_kind"] == "project"
    assert state["invoker"]["launch_mode"] == "mock_no_child_process"
    assert state["workspace"]["effective_cwd"] is None
    assert state["execution"]["workspace_path"] == "/tmp/workspace-a"
    assert state["child_process_environment"]["applied"] is False
    assert state["result"]["result_tree"] == "d" * 40
    assert state["rendered_workspace_files"][0]["invocation_id"] == "a.alpha"
    bundle = state["bundle"]
    assert bundle["sha256"] == hashlib.sha256(bundle_payload).hexdigest()
    assert bundle["artifact"]["relative_path"] == "a/workspace-bundles/a.bundle"


def test_build_node_workspace_descriptor_requires_workspace_state(
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    plan = _workspace_plan()
    output.create_stage_dir("a")

    with pytest.raises(RuntimeError, match="no workspace-state artifact"):
        build_node_workspace_descriptor(plan.nodes[0], plan, output)


def test_build_node_workspace_descriptor_rejects_hardlinked_bundle(
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    plan = _workspace_plan()
    stage_dir = output.create_stage_dir("a")
    outside_bundle = tmp_path / "outside.bundle"
    bundle_payload = b"bundle"
    outside_bundle.write_bytes(bundle_payload)
    bundle_path = stage_dir / "workspace-bundles" / "a.bundle"
    bundle_path.parent.mkdir(parents=True)
    try:
        os.link(outside_bundle, bundle_path)
    except OSError:
        pytest.skip("hardlink creation is unavailable")
    state_path = stage_dir / "workspace-state.json"
    state_path.write_text(
        json.dumps(_workspace_state_payload(plan, bundle_payload), sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Workspace bundle artifact is missing"):
        build_node_workspace_descriptor(plan.nodes[0], plan, output)


def test_build_node_workspace_descriptor_rejects_symlinked_setup_parent(
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    plan = _workspace_plan()
    stage_dir = output.create_stage_dir("a")
    bundle_payload = b"bundle"
    bundle_path = stage_dir / "workspace-bundles" / "a.bundle"
    bundle_path.parent.mkdir(parents=True)
    bundle_path.write_bytes(bundle_payload)
    outside_setup = tmp_path / "outside-setup"
    outside_setup.mkdir()
    (outside_setup / "setup.json").write_text("{}", encoding="utf-8")
    (outside_setup / "setup.log").write_text("setup\n", encoding="utf-8")
    try:
        (stage_dir / "workspace-setup").symlink_to(
            outside_setup,
            target_is_directory=True,
        )
    except OSError:
        pytest.skip("symlink creation is unavailable")
    payload = _workspace_state_payload(plan, bundle_payload)
    payload["setup"] = {
        "metadata_path": "workspace-setup/setup.json",
        "log_path": "workspace-setup/setup.log",
    }
    state_path = stage_dir / "workspace-state.json"
    state_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    with pytest.raises(RuntimeError, match="Workspace setup artifact is missing"):
        build_node_workspace_descriptor(plan.nodes[0], plan, output)


def test_refresh_node_workspace_descriptor_updates_manifest_from_state(
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    plan = _workspace_plan()
    stage_dir = output.create_stage_dir("a")
    bundle_payload = b"bundle"
    bundle_path = stage_dir / "workspace-bundles" / "a.bundle"
    bundle_path.parent.mkdir(parents=True)
    bundle_path.write_bytes(bundle_payload)
    state_path = stage_dir / "workspace-state.json"
    state_payload = _workspace_state_payload(plan, bundle_payload)
    state_path.write_text(json.dumps(state_payload), encoding="utf-8")
    manifest = make_run_manifest(output.run_id, output.run_key_name)
    descriptor = write_result(output.results_dir, "a-result.md", "a output")
    node_state = make_node_state(manifest, "a", [descriptor]).model_copy(
        update={
            "run_id": output.run_id,
            "run_key_name": output.run_key_name,
            "workspace": build_node_workspace_descriptor(plan.nodes[0], plan, output),
        }
    )
    output.write_node_success_state(node_state)
    state_payload["workspace"]["retention"] = "deleted"
    state_payload["workspace"]["retained_reason"] = None
    state_path.write_text(json.dumps(state_payload), encoding="utf-8")

    refresh_node_workspace_descriptor(plan.nodes[0], plan, output)

    node_manifest_path = next((output.stages_dir / "manifests/nodes").glob("*.json"))
    refreshed = json.loads(node_manifest_path.read_text(encoding="utf-8"))
    workspace_state = refreshed["workspace"]["states"][0]
    assert workspace_state["workspace"]["retention"] == "deleted"
    assert workspace_state["workspace"]["retained_reason"] is None


def _workspace_plan():
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    return plan.model_copy(
        update={
            "nodes": [
                plan.nodes[0].model_copy(update={"workspace_policy": policy}),
                plan.nodes[1],
            ],
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


def _workspace_state_payload(plan, bundle_payload: bytes) -> dict[str, object]:
    source = plan.workspace_source
    assert source is not None
    return {
        "version": SCHEMA_VERSION,
        "run_id": plan.run_id,
        "run_key_name": plan.run_key_name,
        "workflow_name": plan.workflow_name,
        "workflow_signature": plan.workflow_signature,
        "node_id": "a",
        "task_id": "alpha",
        "provider": "codex",
        "role": "executor",
        "round_num": 1,
        "audit_round_num": None,
        "status": "succeeded",
        "workspace_kind": "worktree",
        "logical_worktree_name": "primary",
        "clean_start": "strict",
        "worktree_contract": WORKTREE_CONTRACT_PAYLOAD,
        "git": {
            "object_format": source.object_format,
            "repo_id": source.repository_id,
            "run_base_commit": source.run_base_commit,
            "source_tree": source.source_tree,
            "worktree_config_active": False,
        },
        "source": {
            "kind": "project",
            "node_id": None,
            "commit": source.run_base_commit,
            "tree": source.source_tree,
            "candidate_sequence": None,
        },
        "invoker": {
            "implementation": "mock",
            "honors_cwd": True,
            "launch_mode": "mock_no_child_process",
            "controlled_child_environment": False,
        },
        "invocation_source": {
            "source_kind": "project",
            "source_node_id": None,
            "source_commit": source.run_base_commit,
            "source_tree": source.source_tree,
            "candidate_sequence": None,
        },
        "workspace": {
            "path": None,
            "effective_cwd": None,
            "cache_key": "workspace-a",
            "materialization": "worktree_checkout",
            "writable": True,
            "lineage_producer": True,
            "retention": "retained",
            "retained_reason": "cleanup_on_success_false",
            "project_root_relative_path": ".",
        },
        "execution": {
            "cache_root": "/tmp/crewplane-cache",
            "workspace_path": "/tmp/workspace-a",
            "checkout_root": "/tmp/workspace-a/checkout",
            "effective_cwd": "/tmp/workspace-a/checkout",
        },
        "child_process_environment": {"required": True, "applied": False},
        "result": {
            "candidate_commit": "a" * 40,
            "result_commit": "b" * 40,
            "candidate_tree": "c" * 40,
            "result_tree": "d" * 40,
            "changed_path_count": 1,
            "empty_result": False,
            "final_head": "b" * 40,
            "unreachable_provider_objects_scanned": False,
        },
        "refs": {
            "candidate": "refs/crewplane/runs/run/a/a/candidate",
            "result": "refs/crewplane/runs/run/a/a/result",
        },
        "bundle": {
            "path": "a/workspace-bundles/a.bundle",
            "sha256": hashlib.sha256(bundle_payload).hexdigest(),
            "size_bytes": len(bundle_payload),
            "verified": True,
        },
        "rendered_workspace_files": [
            {
                "occurrence_id": "a:prompt:file:src/app.py",
                "invocation_id": "a.alpha",
                "source_kind": "project",
                "source_commit": source.run_base_commit,
                "source_tree": source.source_tree,
                "workspace_relative_path": "src/app.py",
                "injected_sha256": hashlib.sha256(b"content").hexdigest(),
            }
        ],
        "diagnostics": [],
        "updated_at": "2026-06-09T12:00:00+00:00",
    }

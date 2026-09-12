from __future__ import annotations

import json
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import NoReturn, cast

import pytest

import crewplane.runtime.workspace.service as workspace_service
import crewplane.runtime.workspace.service.snapshot as workspace_service_snapshot
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.core.workspace.git_policy import WORKSPACE_GIT_CONFIG_OVERLAY
from crewplane.runtime.agent.workspace_environment import (
    workspace_child_environment,
)
from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace.filesystem import (
    remove_workspace_path,
)
from crewplane.runtime.workspace.git import git
from crewplane.runtime.workspace.snapshot import (
    WorkspaceSnapshotPolicy,
    runtime_git_env,
)
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    create_git_repo,
    disabled_workspace_plan,
    read_json_object,
    run_git_text,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)


class ArtifactStoreWithoutProjectRoot:
    def __getattr__(self, name: str) -> NoReturn:
        raise AssertionError(f"Artifact store field was accessed: {name}")


def artifact_store_without_project_root() -> ArtifactStorePort:
    return cast(ArtifactStorePort, ArtifactStoreWithoutProjectRoot())


def test_prepare_snapshot_workspace_materializes_writable_state(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=True)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    assert prepared.workspace_path.parent == (
        cache_root / "snapshots" / source.repository_id / plan.run_key_name
    )
    assert prepared.state_path is not None
    assert prepared.cwd == prepared.workspace_path / "checkout"
    assert (prepared.cwd / "README.md").read_text(encoding="utf-8") == "ready\n"
    assert prepared.invocation_context.workspace is not None
    assert prepared.invocation_context.workspace.cwd == prepared.cwd
    assert prepared.invocation_context.workspace.child_environment_required is True

    running_state = read_json_object(prepared.state_path)
    assert running_state["status"] == "running"
    assert running_state["invoker"]["launch_mode"] == "runtime_command_runner"
    assert running_state["invoker"]["controlled_child_environment"] is True
    assert running_state["workspace"]["materialization"] == "snapshot_checkout"
    assert running_state["workspace"]["writable"] is True
    assert running_state["workspace"]["path"] is None
    assert running_state["workspace"]["effective_cwd"] is None
    assert running_state["execution"]["checkout_size_bytes"] >= len("ready\n")
    assert running_state["execution"]["provisioning_duration_seconds"] >= 0

    prepared.mark_succeeded()

    succeeded_state = read_json_object(prepared.state_path)
    assert succeeded_state["status"] == "succeeded"
    assert succeeded_state["child_process_environment"]["applied"] is True
    assert succeeded_state["workspace"]["retention"] == "deleted"
    assert succeeded_state["result"] == {
        "lineage_produced": False,
        "drift_scan_complete": True,
        "snapshot_drift_discarded": False,
        "changed_path_count": 0,
        "changed_paths": [],
        "changed_paths_truncated": False,
    }
    assert not prepared.workspace_path.exists()


@pytest.mark.parametrize(
    "policy",
    [
        WorkspaceSnapshotPolicy(max_entries=1),
        WorkspaceSnapshotPolicy(max_file_bytes=1),
        WorkspaceSnapshotPolicy(max_elapsed_seconds=0.000_001),
    ],
)
def test_snapshot_reporting_limits_still_terminalize_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: WorkspaceSnapshotPolicy,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", cleanup_on_success=True)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    (prepared.cwd / "reporting-limit.txt").write_text("limit", encoding="utf-8")
    monkeypatch.setattr(
        "crewplane.runtime.workspace.prepared_workspace.WorkspaceSnapshotPolicy",
        lambda cancel_requested=None: WorkspaceSnapshotPolicy(
            max_entries=policy.max_entries,
            max_file_bytes=policy.max_file_bytes,
            max_elapsed_seconds=policy.max_elapsed_seconds,
            cancel_requested=cancel_requested,
        ),
    )

    prepared.mark_succeeded()

    assert prepared.state_path is not None
    state = read_json_object(prepared.state_path)
    assert state["status"] == "succeeded"
    assert state["result"]["drift_scan_complete"] is False
    assert state["workspace"]["retention"] == "deleted"
    assert prepared.workspace_path is not None
    assert not prepared.workspace_path.exists()


def test_snapshot_workspace_materializes_only_project_subtree(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    project_root = repo / "project"
    sibling_root = repo / "sibling"
    project_root.mkdir()
    sibling_root.mkdir()
    (project_root / "app.txt").write_text("project source\n", encoding="utf-8")
    (sibling_root / "secret.txt").write_text("sibling source\n", encoding="utf-8")
    run_git_text(repo, "add", "project/app.txt", "sibling/secret.txt")
    run_git_text(repo, "commit", "-m", "add project subtree")
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=False)
    source = plan.workspace_source
    assert source is not None
    plan = plan.model_copy(
        update={
            "context_root": project_root.as_posix(),
            "workspace_source": source.model_copy(
                update={"project_root_relative_path": "project"}
            ),
        }
    )
    output = workspace_output_manager(tmp_path, project_root)
    output.create_node_dir(node_artifact_request("implement"))

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )

    assert prepared.workspace_path is not None
    checkout_root = prepared.workspace_path / "checkout"
    assert prepared.cwd == checkout_root / "project"
    assert (prepared.cwd / "app.txt").read_text(encoding="utf-8") == "project source\n"
    assert not (checkout_root / "README.md").exists()
    assert not (checkout_root / "sibling" / "secret.txt").exists()
    assert not (prepared.workspace_path / "index").exists()
    remove_workspace_path(prepared.workspace_path)


def test_snapshot_workspace_discards_provider_mutation(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=False)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.state_path is not None
    readme = prepared.cwd / "README.md"
    readme.chmod(0o600)
    readme.write_text("changed\n", encoding="utf-8")

    prepared.mark_succeeded()

    succeeded_state = read_json_object(prepared.state_path)
    assert succeeded_state["status"] == "succeeded"
    assert succeeded_state["workspace"]["retention"] == "retained"
    assert succeeded_state["result"]["snapshot_drift_discarded"] is True
    assert succeeded_state["result"]["changed_path_count"] == 1
    assert succeeded_state["result"]["changed_paths"] == ["README.md"]
    assert succeeded_state["result"]["changed_paths_truncated"] is False
    assert succeeded_state["diagnostics"] == [
        {
            "level": "warning",
            "message": "Snapshot checkout changes were discarded (1 path(s)).",
        }
    ]
    assert "severity" not in succeeded_state["diagnostics"][0]
    remove_workspace_path(prepared.workspace_path)


def test_snapshot_workspace_retry_reset_restores_initial_checkout(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=False)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.invocation_context.retry_reset is not None
    (prepared.cwd / "README.md").write_text("dirty\n", encoding="utf-8")
    (prepared.cwd / "scratch.txt").write_text("scratch\n", encoding="utf-8")

    prepared.invocation_context.retry_reset()

    assert (prepared.cwd / "README.md").read_text(encoding="utf-8") == "ready\n"
    assert not (prepared.cwd / "scratch.txt").exists()
    prepared.mark_succeeded()
    state = read_json_object(prepared.state_path)
    assert state["result"]["snapshot_drift_discarded"] is False
    remove_workspace_path(prepared.workspace_path)


def test_snapshot_retry_reset_rejects_replaced_workspace_parent(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=False)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.invocation_context.retry_reset is not None
    workspace_path = prepared.workspace_path
    outside = tmp_path / "outside"
    outside.mkdir()
    external_checkout = outside / "checkout"
    external_checkout.mkdir()
    (external_checkout / "keep.txt").write_text("keep\n", encoding="utf-8")
    shutil.rmtree(workspace_path)
    try:
        workspace_path.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(RuntimeError, match="Snapshot workspace retry reset failed"):
        prepared.invocation_context.retry_reset()

    assert (external_checkout / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    remove_workspace_path(workspace_path)


@pytest.mark.parametrize("mutation_kind", ["workspace", "premature_terminal"])
def test_snapshot_workspace_final_state_rejects_state_identity_mutation(
    tmp_path: Path,
    mutation_kind: str,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=False)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.state_path is not None
    mutated_state = read_json_object(prepared.state_path)
    if mutation_kind == "workspace":
        workspace = mutated_state["workspace"]
        assert isinstance(workspace, dict)
        workspace["cache_key"] = "provider-mutated-cache"
    else:
        mutated_state["status"] = "succeeded"
        mutated_state["result"] = {"provider_forged": True}
    prepared.state_path.write_text(json.dumps(mutated_state), encoding="utf-8")

    with pytest.raises(RuntimeError, match="state identity changed"):
        prepared.mark_succeeded()

    assert prepared.workspace_path.exists()
    assert read_json_object(prepared.state_path) == mutated_state
    remove_workspace_path(prepared.workspace_path)


def test_disabled_workspace_uses_project_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = disabled_workspace_plan(repo).model_copy(
        update={"context_root": (tmp_path / ".crewplane" / "stages").as_posix()}
    )
    context = workspace_invocation_context()

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, artifact_store_without_project_root()),
        context,
    )

    assert prepared.cwd == repo
    assert prepared.invocation_context is context
    assert prepared.workspace_path is None
    assert prepared.state_path is None


def test_project_root_workspace_policy_uses_plan_project_root(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", cleanup_on_success=False)
    node = plan.nodes[0]
    policy = node.workspace_policy
    assert policy is not None
    project_root_node = node.model_copy(
        update={
            "workspace_policy": policy.model_copy(
                update={
                    "enabled": False,
                    "logical_worktree_name": None,
                    "declaration_kind": None,
                    "materialization": "project_root",
                    "writable": False,
                    "lineage_producer": False,
                }
            )
        }
    )
    plan = plan.model_copy(
        update={
            "context_root": (tmp_path / ".crewplane" / "stages").as_posix(),
            "nodes": [project_root_node],
        }
    )
    context = workspace_invocation_context()

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, artifact_store_without_project_root()),
        context,
    )

    assert prepared.cwd == repo
    assert prepared.invocation_context is context
    assert prepared.workspace_path is None
    assert prepared.state_path is None


def test_materialization_limit_serializes_snapshot_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", cleanup_on_success=False)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    limiter = workspace_service.MaterializationLimiter.from_plan(plan)
    active = 0
    max_active = 0
    lock = Lock()

    def fake_create_snapshot_workspace(
        plan_arg: object,
        slug: str,
        source: object,
    ) -> Path:
        del plan_arg, source
        nonlocal active, max_active
        workspace_path = tmp_path / "materialized" / slug
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.2)
            (workspace_path / "checkout").mkdir(parents=True)
            return workspace_path
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(
        workspace_service_snapshot,
        "create_snapshot_workspace",
        fake_create_snapshot_workspace,
    )

    def materialize_snapshot(
        source: object,
        checkout_root: Path,
        index_path: Path,
    ) -> None:
        del source, checkout_root, index_path

    monkeypatch.setattr(
        workspace_service_snapshot,
        "materialize_snapshot",
        materialize_snapshot,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                prepare_invocation_workspace,
                workspace_invocation_request(
                    plan,
                    output,
                    audit_round_num=audit_round,
                    materialization_limiter=limiter,
                ),
                workspace_invocation_context(),
            )
            for audit_round in (1, 2)
        ]
        prepared = [future.result() for future in futures]

    assert max_active == 1
    for workspace in prepared:
        assert workspace.workspace_path is not None
        remove_workspace_path(workspace.workspace_path)


def test_snapshot_runtime_git_env_uses_full_sanitizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_WORK_TREE", "/outside")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/tmp/hooks")
    monkeypatch.setenv("GIT_GLOB_PATHSPECS", "1")

    env = runtime_git_env(tmp_path / "operation.index")

    assert env["GIT_INDEX_FILE"] == (tmp_path / "operation.index").as_posix()
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert "GIT_WORK_TREE" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert "GIT_CONFIG_KEY_0" not in env
    assert "GIT_CONFIG_VALUE_0" not in env
    assert "GIT_GLOB_PATHSPECS" not in env


def test_runtime_git_command_applies_workspace_config_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_command: list[str] = []

    def fake_run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        captured_command[:] = command
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    git(tmp_path).run("status", "--short")

    assert captured_command[:2] == ["git", "-c"]
    assert "-C" in captured_command
    for key, value in WORKSPACE_GIT_CONFIG_OVERLAY:
        assert f"{key}={value}" in captured_command


def test_workspace_child_environment_applies_config_overlay(tmp_path: Path) -> None:
    environment = workspace_child_environment(tmp_path)

    assert environment.set["GIT_CONFIG_COUNT"] == str(len(WORKSPACE_GIT_CONFIG_OVERLAY))
    for index, (key, value) in enumerate(WORKSPACE_GIT_CONFIG_OVERLAY):
        assert environment.set[f"GIT_CONFIG_KEY_{index}"] == key
        assert environment.set[f"GIT_CONFIG_VALUE_{index}"] == value

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from crewplane.cli.run.workspace.disk_policy import (
    estimated_checkout_size_bytes as preflight_estimated_checkout_size,
)
from crewplane.cli.run.workspace.disk_policy import (
    estimated_git_checkout_size_bytes,
)
from crewplane.cli.run.workspace.git_source import GitSourceContext
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.workspace.invocation_identity import invocation_slug
from crewplane.runtime.workspace.materialization import (
    MaterializationLimiter,
    estimated_checkout_size,
    workspace_materialization_slot,
)
from crewplane.runtime.workspace.worktree.materialization import (
    materialize_worktree_workspace,
)
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from tests.helpers.workspace_service import (
    create_git_repo,
    file_sha256,
    git_commit_exists,
    run_git_text,
    workspace_plan,
)


def test_runtime_and_preflight_use_the_same_tree_checkout_estimate(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", cleanup_on_success=True)
    source = plan.workspace_source
    assert source is not None
    context = GitSourceContext(
        run_base_commit=source.run_base_commit,
        source_tree=source.source_tree,
        object_format=source.object_format,
        git_top_level=Path(source.git_top_level),
        project_root_relative_path=source.project_root_relative_path,
        active_git_dir=Path(source.active_git_dir),
        common_git_dir=Path(source.common_git_dir),
        git_version=source.git_version,
    )

    assert estimated_checkout_size(
        source, estimate_full_repository=True
    ) == estimated_git_checkout_size_bytes(
        context,
        estimate_full_repository=True,
    )


def test_runtime_fallback_matches_project_scan_and_keeps_tracked_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = create_git_repo(tmp_path)
    project = repo / "nested"
    project.mkdir()
    (project / "app.txt").write_text("app", encoding="utf-8")
    config = project / ".crewplane" / "config.yml"
    config.parent.mkdir()
    config.write_bytes(b"x" * 4096)
    stages = project / ".crewplane" / "execution-stages" / "old"
    stages.mkdir(parents=True)
    (stages / "ignored.bin").write_bytes(b"y" * 8192)
    run_git_text(repo, "add", ".")
    run_git_text(repo, "commit", "-m", "nested project")
    plan = workspace_plan(project, tmp_path / "cache", cleanup_on_success=True)
    source = plan.workspace_source
    assert source is not None
    source = source.model_copy(
        update={
            "git_top_level": repo.as_posix(),
            "project_root_relative_path": "nested",
        }
    )

    def fail_git(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise subprocess.CalledProcessError(1, "git")

    monkeypatch.setattr(
        "crewplane.runtime.workspace.materialization.git",
        fail_git,
    )

    assert estimated_checkout_size(source, estimate_full_repository=False) == 4099


def test_nested_worktree_runtime_estimate_matches_full_repository_preflight(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    (repo / "outside.bin").write_bytes(b"x" * 2048)
    project = repo / "nested"
    project.mkdir()
    (project / "app.bin").write_bytes(b"y" * 4096)
    run_git_text(repo, "add", ".")
    run_git_text(repo, "commit", "-m", "nested project")
    plan = workspace_plan(project, tmp_path / "cache", cleanup_on_success=True)
    source = plan.workspace_source
    assert source is not None
    source = source.model_copy(
        update={
            "git_top_level": repo.as_posix(),
            "project_root_relative_path": "nested",
        }
    )
    context = GitSourceContext(
        run_base_commit=source.run_base_commit,
        source_tree=source.source_tree,
        object_format=source.object_format,
        git_top_level=Path(source.git_top_level),
        project_root_relative_path=source.project_root_relative_path,
        active_git_dir=Path(source.active_git_dir),
        common_git_dir=Path(source.common_git_dir),
        git_version=source.git_version,
    )

    assert estimated_checkout_size(
        source, estimate_full_repository=True
    ) == estimated_git_checkout_size_bytes(
        context,
        estimate_full_repository=True,
    )


def test_nested_worktree_runtime_fallback_scans_full_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = create_git_repo(tmp_path)
    (repo / "outside.bin").write_bytes(b"x" * 2048)
    project = repo / "nested"
    project.mkdir()
    (project / "app.bin").write_bytes(b"y" * 4096)
    run_git_text(repo, "add", ".")
    run_git_text(repo, "commit", "-m", "nested project")
    plan = workspace_plan(project, tmp_path / "cache", cleanup_on_success=True)
    source = plan.workspace_source
    assert source is not None

    source = source.model_copy(
        update={
            "git_top_level": repo.as_posix(),
            "project_root_relative_path": "nested",
        }
    )

    def fail_git(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise subprocess.CalledProcessError(1, "git")

    monkeypatch.setattr("crewplane.runtime.workspace.materialization.git", fail_git)
    monkeypatch.setattr(
        "crewplane.cli.run.workspace.disk_policy.git_zero_records",
        fail_git,
    )
    context = GitSourceContext(
        run_base_commit=source.run_base_commit,
        source_tree=source.source_tree,
        object_format=source.object_format,
        git_top_level=Path(source.git_top_level),
        project_root_relative_path=source.project_root_relative_path,
        active_git_dir=Path(source.active_git_dir),
        common_git_dir=Path(source.common_git_dir),
        git_version=source.git_version,
    )

    assert estimated_checkout_size(
        source, estimate_full_repository=True
    ) == preflight_estimated_checkout_size(
        context,
        estimate_full_repository=True,
    )


def test_nested_fresh_worktree_admission_uses_full_repository_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = create_git_repo(tmp_path)
    (repo / "outside.bin").write_bytes(b"x" * 4096)
    project = repo / "nested"
    project.mkdir()
    (project / "app.bin").write_bytes(b"y" * 512)
    run_git_text(repo, "add", ".")
    run_git_text(repo, "commit", "-m", "nested project")
    plan = workspace_plan(project, tmp_path / "cache", cleanup_on_success=True)
    source = plan.workspace_source
    assert source is not None
    source = source.model_copy(
        update={
            "git_top_level": repo.as_posix(),
            "project_root_relative_path": "nested",
        }
    )
    full_estimate = estimated_checkout_size(source, estimate_full_repository=True)
    project_estimate = estimated_checkout_size(source, estimate_full_repository=False)
    assert full_estimate > project_estimate
    plan = _plan_with_disk_thresholds(
        plan.model_copy(update={"workspace_source": source}),
        fail_free_bytes=1,
    )

    class FailingGit:
        def zero_records(self, *args: str) -> tuple[bytes, ...]:
            del args
            raise subprocess.CalledProcessError(1, "git")

    def failing_git(path: Path) -> FailingGit:
        del path
        return FailingGit()

    def constrained_disk_usage(path: Path) -> SimpleNamespace:
        del path
        return SimpleNamespace(free=project_estimate + 1)

    def unexpected_materialization(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return object()

    monkeypatch.setattr(
        "crewplane.runtime.workspace.materialization.git",
        failing_git,
    )
    monkeypatch.setattr(
        "crewplane.runtime.workspace.materialization.shutil.disk_usage",
        constrained_disk_usage,
    )
    monkeypatch.setattr(
        "crewplane.runtime.workspace.worktree.materialization.create_worktree_workspace",
        unexpected_materialization,
    )
    source_ref = WorktreeSourceRef(
        source_kind="project",
        source_node_id=None,
        source_commit=source.run_base_commit,
        source_tree=source.source_tree,
    )

    with pytest.raises(RuntimeError, match="fail_free_bytes"):
        materialize_worktree_workspace(
            plan,
            "a-executor",
            source,
            source_ref,
            (),
            None,
            None,
            False,
            None,
            materialization_limiter=MaterializationLimiter.from_plan(plan),
            planned_workspace_path=tmp_path / "cache" / "a-executor",
        )


def test_fresh_worktree_admission_uses_selected_lineage_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", cleanup_on_success=True)
    source = plan.workspace_source
    assert source is not None
    base_estimate = estimated_checkout_size(source, estimate_full_repository=True)
    (repo / "upstream.bin").write_bytes(b"x" * 4096)
    run_git_text(repo, "add", "upstream.bin")
    run_git_text(repo, "commit", "-m", "larger upstream")
    upstream_commit = run_git_text(repo, "rev-parse", "HEAD^{commit}")
    upstream_tree = run_git_text(repo, "rev-parse", "HEAD^{tree}")
    upstream_ref = "refs/crewplane/test/capacity-selected-lineage"
    bundle_path = tmp_path / "capacity-selected-lineage.bundle"
    run_git_text(repo, "update-ref", upstream_ref, upstream_commit)
    run_git_text(repo, "bundle", "create", bundle_path.as_posix(), upstream_ref)
    source_ref = WorktreeSourceRef(
        source_kind="node",
        source_node_id="upstream",
        source_commit=upstream_commit,
        source_tree=upstream_tree,
        candidate_sequence=1,
        bundle_path=bundle_path,
        bundle_sha256=file_sha256(bundle_path),
        bundle_size_bytes=bundle_path.stat().st_size,
        bundle_ref=upstream_ref,
        upstream_sources=(
            WorktreeSourceRef(
                source_kind="project",
                source_node_id=None,
                source_commit=source.run_base_commit,
                source_tree=source.source_tree,
            ),
        ),
    )
    lineage_estimate = estimated_checkout_size(
        source,
        estimate_full_repository=True,
        source_tree=source_ref.source_tree,
    )
    assert lineage_estimate > base_estimate
    plan = _plan_with_disk_thresholds(plan, fail_free_bytes=1)
    state_path = tmp_path / "workspace-state.json"
    state_path.write_text(
        json.dumps(
            {
                "run_id": plan.run_id,
                "run_key_name": plan.run_key_name,
                "node_id": "implement",
                "task_id": "alpha",
                "role": "executor",
                "round_num": 1,
                "audit_round_num": None,
                "git": {"repo_id": source.repository_id},
            }
        ),
        encoding="utf-8",
    )

    def constrained_disk_usage(path: Path) -> SimpleNamespace:
        del path
        return SimpleNamespace(free=base_estimate + 1)

    def unexpected_materialization(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return object()

    monkeypatch.setattr(
        "crewplane.runtime.workspace.materialization.shutil.disk_usage",
        constrained_disk_usage,
    )
    monkeypatch.setattr(
        "crewplane.runtime.workspace.worktree.materialization.create_worktree_workspace",
        unexpected_materialization,
    )

    with pytest.raises(RuntimeError, match="fail_free_bytes"):
        materialize_worktree_workspace(
            plan,
            "downstream-executor",
            source,
            source_ref,
            (),
            None,
            None,
            False,
            None,
            materialization_limiter=MaterializationLimiter.from_plan(plan),
            planned_workspace_path=tmp_path / "cache" / "downstream-executor",
            state_path=state_path,
        )


def test_fresh_worktree_admission_imports_bundle_only_lineage_before_estimate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", cleanup_on_success=True)
    source = plan.workspace_source
    assert source is not None
    base_estimate = estimated_checkout_size(source, estimate_full_repository=True)
    (repo / "upstream.bin").write_bytes(b"x" * 4096)
    run_git_text(repo, "add", "upstream.bin")
    upstream_tree = run_git_text(repo, "write-tree")
    upstream_commit = run_git_text(
        repo,
        "commit-tree",
        upstream_tree,
        "-p",
        source.run_base_commit,
        "-m",
        "larger upstream",
    )
    upstream_ref = "refs/crewplane/test/capacity-upstream"
    bundle_path = tmp_path / "capacity-upstream.bundle"
    run_git_text(repo, "update-ref", upstream_ref, upstream_commit)
    run_git_text(repo, "bundle", "create", bundle_path.as_posix(), upstream_ref)
    run_git_text(repo, "reset", "--hard", source.run_base_commit)
    run_git_text(repo, "update-ref", "-d", upstream_ref)
    run_git_text(repo, "reflog", "expire", "--expire=now", "--all")
    run_git_text(repo, "gc", "--prune=now")
    if git_commit_exists(repo, upstream_commit):
        pytest.skip("git retained the test commit after pruning")
    source_ref = WorktreeSourceRef(
        source_kind="node",
        source_node_id="upstream",
        source_commit=upstream_commit,
        source_tree=upstream_tree,
        candidate_sequence=1,
        bundle_path=bundle_path,
        bundle_sha256=file_sha256(bundle_path),
        bundle_size_bytes=bundle_path.stat().st_size,
        bundle_ref=upstream_ref,
        upstream_sources=(
            WorktreeSourceRef(
                source_kind="project",
                source_node_id=None,
                source_commit=source.run_base_commit,
                source_tree=source.source_tree,
            ),
        ),
    )
    plan = _plan_with_disk_thresholds(plan, fail_free_bytes=1)
    state_path = tmp_path / "workspace-state.json"
    state_path.write_text(
        json.dumps(
            {
                "run_id": plan.run_id,
                "run_key_name": plan.run_key_name,
                "node_id": "implement",
                "task_id": "alpha",
                "role": "executor",
                "round_num": 1,
                "audit_round_num": None,
                "git": {"repo_id": source.repository_id},
            }
        ),
        encoding="utf-8",
    )

    def constrained_disk_usage(path: Path) -> SimpleNamespace:
        del path
        return SimpleNamespace(free=base_estimate + 1)

    def unexpected_materialization(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return object()

    monkeypatch.setattr(
        "crewplane.runtime.workspace.materialization.shutil.disk_usage",
        constrained_disk_usage,
    )
    monkeypatch.setattr(
        "crewplane.runtime.workspace.worktree.materialization.create_worktree_workspace",
        unexpected_materialization,
    )

    with pytest.raises(RuntimeError, match="fail_free_bytes"):
        materialize_worktree_workspace(
            plan,
            invocation_slug("implement", "alpha", None, 1),
            source,
            source_ref,
            (),
            None,
            None,
            False,
            None,
            materialization_limiter=MaterializationLimiter.from_plan(plan),
            planned_workspace_path=tmp_path / "cache" / "downstream-executor",
            state_path=state_path,
        )

    assert git_commit_exists(repo, upstream_commit)
    assert (
        run_git_text(
            repo,
            "for-each-ref",
            f"refs/crewplane/runs/{plan.run_key_name}/imports",
        )
        == ""
    )


def test_capacity_admission_reprobes_and_applies_current_free_space(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = _plan_with_disk_thresholds(
        workspace_plan(repo, tmp_path / "cache", cleanup_on_success=True),
        fail_free_bytes=10,
    )
    source = plan.workspace_source
    assert source is not None
    observations = iter((100, 10))

    def next_free_bytes(path: Path) -> SimpleNamespace:
        del path
        return SimpleNamespace(free=next(observations))

    monkeypatch.setattr(
        "crewplane.runtime.workspace.materialization.shutil.disk_usage",
        next_free_bytes,
    )
    limiter = MaterializationLimiter.from_plan(plan)
    target = tmp_path / "cache" / "new" / "checkout"

    with workspace_materialization_slot(plan, limiter, target, source):
        pass
    with (
        pytest.raises(RuntimeError, match="fail_free_bytes"),
        workspace_materialization_slot(plan, limiter, target, source),
    ):
        pass

    assert limiter.admitted_estimated_bytes == 0


def test_capacity_admission_accounts_for_an_in_flight_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = _plan_with_disk_thresholds(
        workspace_plan(repo, tmp_path / "cache", cleanup_on_success=True),
        fail_free_bytes=4,
    )
    source = plan.workspace_source
    assert source is not None
    estimate = estimated_checkout_size(source)

    def free_bytes_for_one_checkout(path: Path) -> SimpleNamespace:
        del path
        return SimpleNamespace(free=(estimate * 2) + 3)

    monkeypatch.setattr(
        "crewplane.runtime.workspace.materialization.shutil.disk_usage",
        free_bytes_for_one_checkout,
    )
    limiter = MaterializationLimiter.from_plan(plan)
    target = tmp_path / "cache" / "new" / "checkout"

    with workspace_materialization_slot(plan, limiter, target, source):
        assert limiter.admitted_estimated_bytes == estimate
        with (
            pytest.raises(RuntimeError, match="fail_free_bytes"),
            workspace_materialization_slot(plan, limiter, target, source),
        ):
            pass

    assert limiter.admitted_estimated_bytes == 0


def test_capacity_probe_failure_is_advisory_without_a_failure_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", cleanup_on_success=True)
    source = plan.workspace_source
    assert source is not None

    def fail_probe(path: Path) -> None:
        del path
        raise OSError("probe unavailable")

    monkeypatch.setattr(
        "crewplane.runtime.workspace.materialization.shutil.disk_usage",
        fail_probe,
    )
    limiter = MaterializationLimiter.from_plan(plan)

    with (
        caplog.at_level(logging.WARNING),
        workspace_materialization_slot(
            plan,
            limiter,
            tmp_path / "cache" / "new",
            source,
        ),
    ):
        pass

    assert "continuing without a configured failure threshold" in caplog.text
    assert limiter.admitted_estimated_bytes == 0


def test_capacity_probe_failure_is_fatal_with_a_failure_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = _plan_with_disk_thresholds(
        workspace_plan(repo, tmp_path / "cache", cleanup_on_success=True),
        fail_free_bytes=1,
    )
    source = plan.workspace_source
    assert source is not None

    def fail_probe(path: Path) -> None:
        del path
        raise OSError("probe unavailable")

    monkeypatch.setattr(
        "crewplane.runtime.workspace.materialization.shutil.disk_usage",
        fail_probe,
    )

    with (
        pytest.raises(RuntimeError, match="capacity probe failed"),
        workspace_materialization_slot(
            plan,
            MaterializationLimiter.from_plan(plan),
            tmp_path / "cache" / "new",
            source,
        ),
    ):
        pass


def _plan_with_disk_thresholds(
    plan: PreflightExecutionPlan,
    **thresholds: int,
) -> PreflightExecutionPlan:
    runtime_snapshot = dict(plan.runtime_config_snapshot)
    workspace = dict(runtime_snapshot["workspace"])
    workspace["disk"] = thresholds
    workspace["max_concurrent_materializations"] = 2
    runtime_snapshot["workspace"] = workspace
    return plan.model_copy(update={"runtime_config_snapshot": runtime_snapshot})

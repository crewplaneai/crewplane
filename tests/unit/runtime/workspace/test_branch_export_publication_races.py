from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.core.preflight.models import WorkspaceSourceSnapshot
from crewplane.runtime.workspace.branch_export import attempts as branch_export_attempts
from crewplane.runtime.workspace.branch_export import (
    fulfill_branch_exports,
)
from crewplane.runtime.workspace.branch_export import git as branch_export_git
from crewplane.runtime.workspace.worktree import (
    temporary_refs as worktree_temporary_refs,
)
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_branch_export import (
    branch_export_plan,
    write_result_bundle,
    write_result_bundle_from_clone,
    write_workspace_state,
)
from tests.helpers.workspace_service import (
    create_git_repo,
    run_git_text,
)
from tests.unit.runtime.workspace.branch_export_support import (
    export_record_path,
)


def test_fulfill_branch_exports_preserves_prepared_record_when_import_ref_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = (
        write_result_bundle_from_clone(
            repo,
            tmp_path,
            output.create_node_dir(node_artifact_request("implement")),
            "feature result\n",
        )
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    original_mark_removed = worktree_temporary_refs.mark_workspace_temporary_ref_removed

    def fail_after_temporary_ref_cleanup(
        state_path: Path,
        ref_name: str,
    ) -> None:
        original_mark_removed(state_path, ref_name)
        raise RuntimeError("injected temporary ref cleanup failure")

    monkeypatch.setattr(
        worktree_temporary_refs,
        "mark_workspace_temporary_ref_removed",
        fail_after_temporary_ref_cleanup,
    )

    with pytest.raises(RuntimeError, match="temporary ref cleanup failure"):
        fulfill_branch_exports(plan, output)

    record_path = export_record_path(output.stages_dir)
    prepared = json.loads(record_path.read_text(encoding="utf-8"))
    assert prepared["status"] == "prepared"
    assert run_git_text(repo, "rev-parse", "refs/heads/feature/exported") == (
        result_commit
    )

    monkeypatch.setattr(
        worktree_temporary_refs,
        "mark_workspace_temporary_ref_removed",
        original_mark_removed,
    )
    records = fulfill_branch_exports(plan, output)

    recovered = json.loads(records[0].read_text(encoding="utf-8"))
    assert recovered["status"] == "fulfilled"
    assert recovered["operation"] == "verified_existing"
    assert recovered["recovery_mode"] == "prepared_record"


def test_fulfill_branch_exports_preserves_prepared_record_when_branch_lock_teardown_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    original_lock = branch_export_git.git_metadata_lock

    @contextmanager
    def fail_after_branch_lock_teardown(common_git_dir: Path) -> Iterator[None]:
        with original_lock(common_git_dir):
            yield
        raise RuntimeError("injected branch lock teardown failure")

    monkeypatch.setattr(
        branch_export_git,
        "git_metadata_lock",
        fail_after_branch_lock_teardown,
    )

    with pytest.raises(RuntimeError, match="branch lock teardown failure"):
        fulfill_branch_exports(plan, output)

    record_path = export_record_path(output.stages_dir)
    prepared = json.loads(record_path.read_text(encoding="utf-8"))
    assert prepared["status"] == "prepared"
    assert run_git_text(repo, "rev-parse", "refs/heads/feature/exported") == (
        result_commit
    )

    monkeypatch.setattr(branch_export_git, "git_metadata_lock", original_lock)
    records = fulfill_branch_exports(plan, output)

    recovered = json.loads(records[0].read_text(encoding="utf-8"))
    assert recovered["status"] == "fulfilled"
    assert recovered["operation"] == "verified_existing"
    assert recovered["recovery_mode"] == "prepared_record"


def test_fulfill_branch_exports_preserves_prepared_record_after_update_ref_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    original_run = branch_export_git.GitCommand.run
    timed_out = False

    def mutate_then_timeout(
        command: branch_export_git.GitCommand,
        *args: str,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal timed_out
        result = original_run(command, *args)
        if (
            args[:3] == ("update-ref", "--no-deref", "refs/heads/feature/exported")
            and not timed_out
        ):
            timed_out = True
            raise subprocess.TimeoutExpired(("git", *args), command.timeout_seconds)
        return result

    monkeypatch.setattr(branch_export_git.GitCommand, "run", mutate_then_timeout)

    with pytest.raises(RuntimeError, match="timed out"):
        fulfill_branch_exports(plan, output)

    record_path = export_record_path(output.stages_dir)
    prepared = json.loads(record_path.read_text(encoding="utf-8"))
    assert prepared["status"] == "prepared"
    assert run_git_text(repo, "rev-parse", "refs/heads/feature/exported") == (
        result_commit
    )

    monkeypatch.setattr(branch_export_git.GitCommand, "run", original_run)
    records = fulfill_branch_exports(plan, output)

    recovered = json.loads(records[0].read_text(encoding="utf-8"))
    assert recovered["status"] == "fulfilled"
    assert recovered["operation"] == "verified_existing"
    assert recovered["recovery_mode"] == "prepared_record"


@pytest.mark.parametrize(
    "race_matches_target",
    [True, False],
    ids=("exact-target", "different-target"),
)
def test_fulfill_branch_exports_terminalizes_current_run_branch_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    race_matches_target: bool,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/raced")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    branch_ref = "refs/heads/feature/raced"
    raced_commit = (
        result_commit
        if race_matches_target
        else run_git_text(repo, "rev-parse", "HEAD^{commit}")
    )
    original_branch_ref_exists = branch_export_attempts.branch_ref_exists
    raced = False

    def create_branch_after_probe(
        source: WorkspaceSourceSnapshot,
        requested_branch_ref: str,
    ) -> bool:
        nonlocal raced
        exists = original_branch_ref_exists(source, requested_branch_ref)
        if not raced:
            assert exists is False
            raced = True
            run_git_text(repo, "update-ref", requested_branch_ref, raced_commit)
        return exists

    monkeypatch.setattr(
        branch_export_attempts,
        "branch_ref_exists",
        create_branch_after_probe,
    )

    for attempt in range(2):
        with pytest.raises(RuntimeError, match="refuses to overwrite"):
            fulfill_branch_exports(plan, output)

        record = json.loads(
            export_record_path(output.stages_dir).read_text(encoding="utf-8")
        )
        assert record["status"] == "failed_verification", f"attempt {attempt + 1}"
        assert record["operation"] == "failed_verification"
        assert record["recovery_mode"] == "initial"

    assert run_git_text(repo, "rev-parse", branch_ref) == raced_commit


def test_create_branch_ref_refuses_raced_existing_branch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/raced")
    source = plan.workspace_source
    assert source is not None
    result_commit = run_git_text(repo, "rev-parse", "HEAD^{commit}")
    branch_ref = "refs/heads/feature/raced"
    branch_commit_calls = 0

    class RacedGitCommand:
        def run(self, *args: str) -> object:
            if args[:2] == ("update-ref", "--no-deref"):
                raise subprocess.CalledProcessError(
                    1,
                    ("git", *args),
                    stderr=b"cannot lock ref",
                )
            raise AssertionError(f"unexpected git command: {args!r}")

    def fake_git(cwd: Path) -> RacedGitCommand:
        assert cwd == repo
        return RacedGitCommand()

    def fake_branch_commit(command: object, ref: str) -> str | None:
        del command
        nonlocal branch_commit_calls
        assert ref == branch_ref
        branch_commit_calls += 1
        return None if branch_commit_calls == 1 else result_commit

    monkeypatch.setattr(branch_export_git, "git", fake_git)
    monkeypatch.setattr(branch_export_git, "branch_commit", fake_branch_commit)

    with pytest.raises(RuntimeError, match="refuses to overwrite existing branch"):
        branch_export_git.create_or_verify_branch_ref(
            source,
            branch_ref,
            result_commit,
        )

    assert branch_commit_calls == 2


@pytest.mark.parametrize("target_exists", [False, True], ids=("dangling", "live"))
def test_create_branch_ref_rejects_symbolic_destination_without_touching_target(
    tmp_path: Path,
    target_exists: bool,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    source = plan.workspace_source
    assert source is not None
    result_commit = run_git_text(repo, "rev-parse", "HEAD^{commit}")
    branch_ref = "refs/heads/feature/exported"
    target_ref = "refs/heads/user-work"
    if target_exists:
        run_git_text(repo, "update-ref", target_ref, result_commit)
    run_git_text(repo, "symbolic-ref", branch_ref, target_ref)

    with pytest.raises(RuntimeError, match="symbolic"):
        branch_export_git.create_or_verify_branch_ref(
            source,
            branch_ref,
            result_commit,
            allow_existing=True,
        )

    assert run_git_text(repo, "symbolic-ref", branch_ref) == target_ref
    assert _optional_ref_oid(repo, target_ref) == (
        result_commit if target_exists else None
    )


def _optional_ref_oid(repo: Path, ref_name: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", repo.as_posix(), "rev-parse", "--verify", ref_name],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None

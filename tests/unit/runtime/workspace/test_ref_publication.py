from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import crewplane.runtime.workspace.worktree.orchestration as worktree_orchestration
import crewplane.runtime.workspace.worktree.ref_publication as ref_publication
from crewplane.core.workspace.invocation_identity import invocation_slug
from crewplane.core.workspace.repository_identity import workspace_repository_id
from crewplane.runtime.workspace import PreparedWorkspace, prepare_invocation_workspace
from crewplane.runtime.workspace.state import (
    discard_workspace_lineage,
    read_workspace_state,
)
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from crewplane.runtime.workspace.worktree.protected_refs import (
    protected_ref_snapshot_for_scopes,
)
from crewplane.runtime.workspace.worktree.ref_cleanup import delete_run_workspace_refs
from crewplane.runtime.workspace.worktree.temporary_refs import TemporaryRefOwner
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    run_git_text,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)


def test_external_ref_cleanup_consumes_dedicated_temporary_ref_evidence(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    run_key_name = "run-1"
    node_id = "consumer"
    task_id = "workspace-file-readme"
    target_oid = run_git_text(repo, "rev-parse", "HEAD^{commit}")
    owner_slug = invocation_slug(node_id, task_id, None, 0)
    ref_name = (
        f"refs/crewplane/runs/{run_key_name}/imports/{node_id}/{owner_slug}/temporary"
    )
    run_git_text(repo, "update-ref", ref_name, target_oid)
    run_dir = tmp_path / "run"
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True)
    evidence_path = logs_dir / "workspace-temporary-refs-interrupted.json"
    repository_id = _repository_id(repo)
    _write_dedicated_ref_evidence(
        evidence_path,
        repository_id,
        run_key_name,
        node_id,
        task_id,
        ref_name,
        target_oid,
    )

    removed = delete_run_workspace_refs(
        repo,
        repo / ".git",
        repo,
        run_key_name,
        run_dir,
    )

    assert removed == 1
    assert _ref_oid(repo, ref_name) is None
    assert not evidence_path.exists()


def test_external_ref_cleanup_consumes_empty_prepared_temporary_ref_evidence(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=False,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    run_dir = tmp_path / "run"
    evidence_dir = run_dir / "logs"
    evidence_dir.mkdir(parents=True)
    owner = TemporaryRefOwner.dedicated(
        plan,
        source,
        evidence_dir,
        "consumer",
        "workspace-file-readme",
    )
    owner.prepare()

    removed = delete_run_workspace_refs(
        repo,
        repo / ".git",
        repo,
        plan.run_key_name,
        run_dir,
    )

    assert removed == 0
    assert not owner.state_path.exists()


def test_external_ref_cleanup_rejects_foreign_repository_evidence(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign_repo = create_git_repo(foreign_root)
    run_key_name = "run-1"
    node_id = "consumer"
    task_id = "workspace-file-readme"
    target_oid = run_git_text(repo, "rev-parse", "HEAD^{commit}")
    owner_slug = invocation_slug(node_id, task_id, None, 0)
    ref_name = (
        f"refs/crewplane/runs/{run_key_name}/imports/{node_id}/{owner_slug}/temporary"
    )
    run_git_text(repo, "update-ref", ref_name, target_oid)
    run_dir = tmp_path / "run"
    evidence_path = run_dir / "logs" / "workspace-temporary-refs-interrupted.json"
    evidence_path.parent.mkdir(parents=True)
    _write_dedicated_ref_evidence(
        evidence_path,
        _repository_id(foreign_repo),
        run_key_name,
        node_id,
        task_id,
        ref_name,
        target_oid,
    )

    with pytest.raises(RuntimeError, match="different repository"):
        delete_run_workspace_refs(
            repo,
            repo / ".git",
            repo,
            run_key_name,
            run_dir,
        )

    assert _ref_oid(repo, ref_name) == target_oid
    assert evidence_path.exists()


def test_external_ref_cleanup_reconciles_nested_planned_stage_state(
    tmp_path: Path,
) -> None:
    repo, prepared, state = _published_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    assert prepared.workspace_path is not None
    run_dir = prepared.state_path.parent.parent
    nested_state_path = run_dir / "custom/build-stage/workspace-state.json"
    nested_state_path.parent.mkdir(parents=True)
    prepared.state_path.replace(nested_state_path)
    plan_path = run_dir / "preflight/execution-plan.json"
    plan_path.parent.mkdir()
    plan_path.write_text(
        json.dumps(
            {
                "run_key_name": "workspace-run-001",
                "nodes": [
                    {
                        "id": "implement",
                        "artifact_contract": {"stage_path": "custom/build-stage"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    removed = delete_run_workspace_refs(
        repo,
        repo / ".git",
        repo,
        "workspace-run-001",
        run_dir,
    )

    assert removed == 2
    refs = state["refs"]
    assert isinstance(refs, dict)
    assert _ref_oid(repo, str(refs["candidate"])) is None
    assert _ref_oid(repo, str(refs["result"])) is None
    remove_worktree_workspace(
        prepared.worktree_capture.source,
        prepared.workspace_path,
        prepared.worktree_capture.git_dir,
    )


@pytest.mark.parametrize("stage_path", [".", "./"])
def test_ref_cleanup_rejects_dot_stage_before_mutating_refs(
    tmp_path: Path, stage_path: str
) -> None:
    repo, prepared, state = _published_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    run_dir = prepared.state_path.parent.parent
    plan_path = run_dir / "preflight/execution-plan.json"
    plan_path.parent.mkdir()
    plan_path.write_text(
        json.dumps(
            {
                "run_key_name": "workspace-run-001",
                "nodes": [{"artifact_contract": {"stage_path": stage_path}}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="unsafe stage evidence"):
        delete_run_workspace_refs(
            repo, repo / ".git", repo, "workspace-run-001", run_dir
        )

    refs = state["refs"]
    assert isinstance(refs, dict)
    assert _ref_oid(repo, str(refs["candidate"])) is not None
    assert _ref_oid(repo, str(refs["result"])) is not None
    _remove_published_workspace(repo, prepared, state)


def test_result_refs_are_published_in_one_transaction_after_prepared_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, prepared = _prepared_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    assert prepared.workspace_path is not None
    state_at_mutation: dict[str, object] | None = None
    transaction_inputs: list[str] = []
    real_git = ref_publication.git

    class TrackingCommand:
        def __init__(self, cwd: Path) -> None:
            self._command = real_git(cwd)

        def text(self, *args: str) -> str:
            return self._command.text(*args)

        def run_with_input(self, stdin: bytes, *args: str):
            nonlocal state_at_mutation
            state_at_mutation = read_json_object(prepared.state_path)
            transaction_inputs.append(stdin.decode("utf-8"))
            return self._command.run_with_input(stdin, *args)

    monkeypatch.setattr(ref_publication, "git", TrackingCommand)

    prepared.mark_succeeded()

    assert state_at_mutation is not None
    assert state_at_mutation["ref_publication"]["phase"] == "prepared"
    assert len(transaction_inputs) == 1
    commands = transaction_inputs[0].splitlines()
    assert commands[0] == "start"
    assert commands[1] == "option no-deref"
    assert commands.count("option no-deref") == 2
    assert [line.split()[0] for line in commands].count("update") == 2
    assert commands[-2:] == ["prepare", "commit"]
    state = read_json_object(prepared.state_path)
    assert state["ref_publication"]["phase"] == "published"
    _remove_published_workspace(repo, prepared, state)


def test_result_ref_transaction_atomically_verifies_consumed_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, prepared = _prepared_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    assert prepared.workspace_path is not None
    capture = prepared.worktree_capture
    assert capture is not None
    consumed_ref = "refs/crewplane/test/consumed"
    expected_oid = capture.source.run_base_commit
    run_git_text(repo, "update-ref", consumed_ref, expected_oid)
    protected_refs = protected_ref_snapshot_for_scopes(
        repo,
        (*capture.protected_refs.scopes, consumed_ref),
    )
    prepared.worktree_capture = replace(capture, protected_refs=protected_refs)
    moved_oid = run_git_text(
        repo,
        "commit-tree",
        capture.source.source_tree,
        "-p",
        expected_oid,
        "-m",
        "moved protected ref",
    )
    original_commit_tree = worktree_orchestration.commit_tree

    def commit_then_move_ref(
        checkout_root: Path,
        tree: str,
        parent: str,
        message: str,
    ) -> str:
        result = original_commit_tree(checkout_root, tree, parent, message)
        run_git_text(repo, "update-ref", consumed_ref, moved_oid)
        return result

    monkeypatch.setattr(worktree_orchestration, "commit_tree", commit_then_move_ref)

    with pytest.raises(subprocess.CalledProcessError):
        prepared.mark_succeeded()

    state = read_json_object(prepared.state_path)
    assert state["ref_publication"]["phase"] == "prepared"
    destinations = state["ref_publication"]["destinations"]
    assert isinstance(destinations, dict)
    for destination in destinations.values():
        assert isinstance(destination, dict)
        assert _ref_oid(repo, str(destination["name"])) is None
    assert _ref_oid(repo, consumed_ref) == moved_oid
    run_git_text(repo, "update-ref", "-d", consumed_ref)
    remove_worktree_workspace(
        capture.source,
        prepared.workspace_path,
        capture.git_dir,
    )


def test_result_ref_cleanup_uses_one_exact_oid_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, prepared, state = _published_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    assert prepared.workspace_path is not None
    transaction_inputs: list[str] = []
    real_git = ref_publication.git

    class TrackingCommand:
        def __init__(self, cwd: Path) -> None:
            self._command = real_git(cwd)

        def text(self, *args: str) -> str:
            return self._command.text(*args)

        def run_with_input(self, stdin: bytes, *args: str):
            transaction_inputs.append(stdin.decode("utf-8"))
            return self._command.run_with_input(stdin, *args)

    monkeypatch.setattr(ref_publication, "git", TrackingCommand)

    removed = ref_publication.reconcile_result_ref_publication(
        prepared.state_path,
        repo,
        repo / ".git",
    )

    assert removed == 2
    assert len(transaction_inputs) == 1
    commands = transaction_inputs[0].splitlines()
    assert commands[0] == "start"
    assert commands[1] == "option no-deref"
    assert commands.count("option no-deref") == 2
    assert [line.split()[0] for line in commands].count("delete") == 2
    assert commands[-2:] == ["prepare", "commit"]
    assert read_json_object(prepared.state_path)["ref_publication"]["phase"] == (
        "removed"
    )
    refs = state["refs"]
    assert isinstance(refs, dict)
    assert _ref_oid(repo, str(refs["candidate"])) is None
    assert _ref_oid(repo, str(refs["result"])) is None
    source = prepared.worktree_capture.source
    remove_worktree_workspace(
        source, prepared.workspace_path, prepared.worktree_capture.git_dir
    )


def test_external_ref_cleanup_rejects_unresolved_finalizer_before_mutation(
    tmp_path: Path,
) -> None:
    repo, prepared, state = _published_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    payload = read_json_object(prepared.state_path)
    payload["workspace_mutator"] = {"status": "unresolved", "kind": "finalizer"}
    prepared.state_path.write_text(json.dumps(payload), encoding="utf-8")
    refs = state["refs"]
    assert isinstance(refs, dict)

    with pytest.raises(RuntimeError, match="unresolved workspace mutator"):
        delete_run_workspace_refs(
            repo,
            repo / ".git",
            repo,
            str(payload["run_key_name"]),
            prepared.state_path.parent.parent,
        )

    assert _ref_oid(repo, str(refs["candidate"])) is not None
    assert _ref_oid(repo, str(refs["result"])) is not None
    payload.pop("workspace_mutator")
    prepared.state_path.write_text(json.dumps(payload), encoding="utf-8")
    _remove_published_workspace(repo, prepared, state)


def test_external_ref_cleanup_accepts_discarded_lineage_publication(
    tmp_path: Path,
) -> None:
    repo, prepared, state = _published_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    assert prepared.workspace_path is not None
    discard_workspace_lineage(prepared.state_path, "no_progress_candidate")
    discarded = read_workspace_state(prepared.state_path)
    refs = state["refs"]
    assert isinstance(refs, dict)

    removed = delete_run_workspace_refs(
        repo,
        repo / ".git",
        repo,
        str(discarded["run_key_name"]),
        prepared.state_path.parent.parent,
    )

    assert removed == 2
    assert _ref_oid(repo, str(refs["candidate"])) is None
    assert _ref_oid(repo, str(refs["result"])) is None
    publication = read_workspace_state(prepared.state_path)["ref_publication"]
    assert isinstance(publication, dict)
    assert publication["phase"] == "removed"
    _remove_published_workspace(repo, prepared, state)


def test_external_ref_cleanup_rejects_contradictory_discarded_publication(
    tmp_path: Path,
) -> None:
    repo, prepared, state = _published_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    discard_workspace_lineage(prepared.state_path, "no_progress_candidate")
    discarded = read_workspace_state(prepared.state_path)
    publication = discarded["ref_publication"]
    assert isinstance(publication, dict)
    publication["repository_id"] = "different-repository"
    prepared.state_path.write_text(json.dumps(discarded), encoding="utf-8")
    refs = state["refs"]
    assert isinstance(refs, dict)

    with pytest.raises(RuntimeError, match="hardening evidence"):
        delete_run_workspace_refs(
            repo,
            repo / ".git",
            repo,
            str(discarded["run_key_name"]),
            prepared.state_path.parent.parent,
        )

    assert _ref_oid(repo, str(refs["candidate"])) is not None
    assert _ref_oid(repo, str(refs["result"])) is not None
    _remove_published_workspace(repo, prepared, state)


@pytest.mark.parametrize("unsafe_kind", ("malformed_archive", "symlinked_state"))
def test_external_ref_cleanup_prevalidates_all_claims_before_mutation(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    repo, prepared, state = _published_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    run_dir = prepared.state_path.parent.parent
    unsafe_path = prepared.state_path.with_name(
        "workspace-reuse-claim-workspace-state-generation-2.json"
        if unsafe_kind == "malformed_archive"
        else "workspace-state-symlink.json"
    )
    if unsafe_kind == "malformed_archive":
        unsafe_path.write_text("not-json", encoding="utf-8")
    else:
        unsafe_path.symlink_to(prepared.state_path)
    refs = state["refs"]
    assert isinstance(refs, dict)

    with pytest.raises(RuntimeError, match="malformed evidence|unsafe evidence"):
        delete_run_workspace_refs(
            repo,
            repo / ".git",
            repo,
            str(read_json_object(prepared.state_path)["run_key_name"]),
            run_dir,
        )

    assert _ref_oid(repo, str(refs["candidate"])) is not None
    assert _ref_oid(repo, str(refs["result"])) is not None
    unsafe_path.unlink()
    _remove_published_workspace(repo, prepared, state)


def test_external_ref_cleanup_rejects_symlinked_claim_directory_before_mutation(
    tmp_path: Path,
) -> None:
    repo, prepared, state = _published_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    run_dir = prepared.state_path.parent.parent
    external_claims = tmp_path / "external-claims"
    external_claims.mkdir()
    (external_claims / "workspace-reuse-claim-malformed.json").write_text(
        "not-json", encoding="utf-8"
    )
    (run_dir / "other-node").symlink_to(external_claims, target_is_directory=True)
    refs = state["refs"]
    assert isinstance(refs, dict)

    with pytest.raises(RuntimeError, match="unsafe evidence"):
        delete_run_workspace_refs(
            repo,
            repo / ".git",
            repo,
            str(read_json_object(prepared.state_path)["run_key_name"]),
            run_dir,
        )

    assert _ref_oid(repo, str(refs["candidate"])) is not None
    assert _ref_oid(repo, str(refs["result"])) is not None
    (run_dir / "other-node").unlink()
    _remove_published_workspace(repo, prepared, state)


def test_external_ref_cleanup_consumes_archived_reuse_claim(tmp_path: Path) -> None:
    repo, prepared, state = _published_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    archived_path = prepared.state_path.with_name(
        "workspace-reuse-claim-workspace-state-generation-2.json"
    )
    shutil.move(prepared.state_path, archived_path)
    payload = read_json_object(archived_path)
    refs = state["refs"]
    assert isinstance(refs, dict)

    removed = delete_run_workspace_refs(
        repo,
        repo / ".git",
        repo,
        str(payload["run_key_name"]),
        archived_path.parent.parent,
    )

    assert removed == 2
    assert _ref_oid(repo, str(refs["candidate"])) is None
    assert _ref_oid(repo, str(refs["result"])) is None
    source = prepared.worktree_capture.source
    remove_worktree_workspace(
        source, prepared.workspace_path, prepared.worktree_capture.git_dir
    )


def test_external_ref_cleanup_ignores_non_authoritative_workspace_like_metadata(
    tmp_path: Path,
) -> None:
    repo, prepared, state = _published_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    assert prepared.workspace_path is not None
    setup_dir = prepared.state_path.parent / "workspace-setup"
    setup_dir.mkdir()
    (setup_dir / "workspace-state-implement-alpha-round1.json").write_text(
        json.dumps({"run_key_name": "setup-metadata"}),
        encoding="utf-8",
    )
    generated_dir = prepared.state_path.parent / "generated-files" / "tool"
    generated_dir.mkdir(parents=True)
    (generated_dir / "workspace-state-report.json").write_text(
        json.dumps({"run_key_name": "generated-output"}),
        encoding="utf-8",
    )
    manifest_dir = prepared.state_path.parent.parent / "manifests" / "nodes"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "workspace-state-forged.json").write_text(
        json.dumps({"run_key_name": "manifest-output"}),
        encoding="utf-8",
    )

    removed = delete_run_workspace_refs(
        repo,
        repo / ".git",
        repo,
        str(read_json_object(prepared.state_path)["run_key_name"]),
        prepared.state_path.parent.parent,
    )

    assert removed == 2
    refs = state["refs"]
    assert isinstance(refs, dict)
    assert _ref_oid(repo, str(refs["candidate"])) is None
    assert _ref_oid(repo, str(refs["result"])) is None
    remove_worktree_workspace(
        prepared.worktree_capture.source,
        prepared.workspace_path,
        prepared.worktree_capture.git_dir,
    )


def test_result_ref_cleanup_leaves_moved_ref_and_preserves_publication_phase(
    tmp_path: Path,
) -> None:
    repo, prepared, state = _published_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    assert prepared.workspace_path is not None
    source = prepared.worktree_capture.source
    refs = state["refs"]
    assert isinstance(refs, dict)
    candidate_ref = str(refs["candidate"])
    result_ref = str(refs["result"])
    run_git_text(repo, "update-ref", candidate_ref, source.run_base_commit)

    with pytest.raises(RuntimeError, match="moved or remain"):
        ref_publication.reconcile_result_ref_publication(
            prepared.state_path,
            repo,
            repo / ".git",
        )

    assert _ref_oid(repo, candidate_ref) == source.run_base_commit
    assert _ref_oid(repo, result_ref) is None
    assert read_json_object(prepared.state_path)["ref_publication"]["phase"] == (
        "published"
    )
    run_git_text(repo, "update-ref", "-d", candidate_ref)
    remove_worktree_workspace(
        source, prepared.workspace_path, prepared.worktree_capture.git_dir
    )


@pytest.mark.parametrize("target_exists", (False, True), ids=("dangling", "live"))
def test_result_ref_cleanup_rejects_symbolic_ref_without_touching_target(
    tmp_path: Path,
    target_exists: bool,
) -> None:
    repo, prepared, state = _published_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    assert prepared.workspace_path is not None
    refs = state["refs"]
    assert isinstance(refs, dict)
    candidate_ref = str(refs["candidate"])
    result_ref = str(refs["result"])
    target_ref = "refs/heads/user-work"
    target_oid = _ref_oid(repo, candidate_ref)
    assert target_oid is not None
    if target_exists:
        run_git_text(repo, "update-ref", target_ref, target_oid)
    run_git_text(repo, "update-ref", "--no-deref", "-d", candidate_ref)
    run_git_text(repo, "symbolic-ref", candidate_ref, target_ref)

    with pytest.raises(RuntimeError, match="symbolic"):
        ref_publication.reconcile_result_ref_publication(
            prepared.state_path,
            repo,
            repo / ".git",
        )

    assert run_git_text(repo, "symbolic-ref", candidate_ref) == target_ref
    assert _ref_oid(repo, target_ref) == (target_oid if target_exists else None)
    assert _ref_oid(repo, result_ref) is not None
    assert read_json_object(prepared.state_path)["ref_publication"]["phase"] == (
        "published"
    )
    run_git_text(repo, "symbolic-ref", "-d", candidate_ref)
    run_git_text(repo, "update-ref", "--no-deref", "-d", result_ref)
    remove_worktree_workspace(
        prepared.worktree_capture.source,
        prepared.workspace_path,
        prepared.worktree_capture.git_dir,
    )


def test_result_ref_cleanup_transaction_failure_preserves_refs_and_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, prepared, state = _published_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    assert prepared.workspace_path is not None
    real_git = ref_publication.git

    class FailingCommand:
        def __init__(self, cwd: Path) -> None:
            self._command = real_git(cwd)

        def text(self, *args: str) -> str:
            return self._command.text(*args)

        def run_with_input(self, stdin: bytes, *args: str):
            del stdin
            raise subprocess.CalledProcessError(1, ("git", *args))

    monkeypatch.setattr(ref_publication, "git", FailingCommand)

    with pytest.raises(subprocess.CalledProcessError):
        ref_publication.reconcile_result_ref_publication(
            prepared.state_path,
            repo,
            repo / ".git",
        )

    refs = state["refs"]
    assert isinstance(refs, dict)
    assert _ref_oid(repo, str(refs["candidate"])) is not None
    assert _ref_oid(repo, str(refs["result"])) is not None
    assert read_json_object(prepared.state_path)["ref_publication"]["phase"] == (
        "published"
    )
    _remove_published_workspace(repo, prepared, state)


def test_result_ref_cleanup_treats_already_absent_refs_as_removed(
    tmp_path: Path,
) -> None:
    repo, prepared, state = _published_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    assert prepared.workspace_path is not None
    refs = state["refs"]
    assert isinstance(refs, dict)
    run_git_text(repo, "update-ref", "-d", str(refs["candidate"]))
    run_git_text(repo, "update-ref", "-d", str(refs["result"]))

    assert (
        ref_publication.reconcile_result_ref_publication(
            prepared.state_path,
            repo,
            repo / ".git",
        )
        == 0
    )
    assert read_json_object(prepared.state_path)["ref_publication"]["phase"] == (
        "removed"
    )
    source = prepared.worktree_capture.source
    remove_worktree_workspace(
        source, prepared.workspace_path, prepared.worktree_capture.git_dir
    )


def test_same_attempt_exact_target_ref_race_fails_with_prepared_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, prepared = _prepared_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    assert prepared.workspace_path is not None
    original_update = ref_publication.update_workspace_ref_publication
    injected_ref: str | None = None

    def persist_then_race(state_path: Path, publication: dict[str, object]) -> None:
        nonlocal injected_ref
        original_update(state_path, publication)
        destinations = publication["destinations"]
        assert isinstance(destinations, dict)
        candidate = destinations["candidate"]
        assert isinstance(candidate, dict)
        injected_ref = str(candidate["name"])
        run_git_text(repo, "update-ref", injected_ref, str(candidate["target_oid"]))

    monkeypatch.setattr(
        ref_publication,
        "update_workspace_ref_publication",
        persist_then_race,
    )

    with pytest.raises(RuntimeError, match="result ref already exists"):
        prepared.mark_succeeded()

    assert injected_ref is not None
    state = read_json_object(prepared.state_path)
    assert state["ref_publication"]["phase"] == "prepared"
    publication = state["ref_publication"]
    destinations = publication["destinations"]
    assert isinstance(destinations, dict)
    assert _ref_oid(repo, str(destinations["result"]["name"])) is None
    run_git_text(repo, "update-ref", "-d", injected_ref)
    source = prepared.worktree_capture.source
    remove_worktree_workspace(
        source, prepared.workspace_path, prepared.worktree_capture.git_dir
    )


@pytest.mark.parametrize("target_exists", (False, True), ids=("dangling", "live"))
def test_result_ref_publication_rejects_symbolic_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_exists: bool,
) -> None:
    repo, prepared = _prepared_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    assert prepared.workspace_path is not None
    original_update = ref_publication.update_workspace_ref_publication
    candidate_ref: str | None = None
    target_ref = "refs/heads/user-work"
    target_oid: str | None = None

    def persist_then_add_symbolic_ref(
        state_path: Path,
        publication: dict[str, object],
    ) -> None:
        nonlocal candidate_ref, target_oid
        original_update(state_path, publication)
        destinations = publication["destinations"]
        assert isinstance(destinations, dict)
        candidate = destinations["candidate"]
        assert isinstance(candidate, dict)
        candidate_ref = str(candidate["name"])
        target_oid = str(candidate["target_oid"])
        if target_exists:
            run_git_text(repo, "update-ref", target_ref, target_oid)
        run_git_text(repo, "symbolic-ref", candidate_ref, target_ref)

    monkeypatch.setattr(
        ref_publication,
        "update_workspace_ref_publication",
        persist_then_add_symbolic_ref,
    )

    with pytest.raises(RuntimeError, match="symbolic"):
        prepared.mark_succeeded()

    assert candidate_ref is not None
    assert target_oid is not None
    assert run_git_text(repo, "symbolic-ref", candidate_ref) == target_ref
    assert _ref_oid(repo, target_ref) == (target_oid if target_exists else None)
    publication = read_json_object(prepared.state_path)["ref_publication"]
    destinations = publication["destinations"]
    assert isinstance(destinations, dict)
    assert _ref_oid(repo, str(destinations["result"]["name"])) is None
    run_git_text(repo, "symbolic-ref", "-d", candidate_ref)
    remove_worktree_workspace(
        prepared.worktree_capture.source,
        prepared.workspace_path,
        prepared.worktree_capture.git_dir,
    )


def test_post_transaction_phase_failure_cleans_exact_refs_and_keeps_prepared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, prepared = _prepared_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    assert prepared.workspace_path is not None

    def fail_phase_update(state_path: Path, phase: str) -> None:
        del state_path, phase
        raise RuntimeError("phase write failed")

    monkeypatch.setattr(
        ref_publication,
        "update_workspace_ref_publication_phase",
        fail_phase_update,
    )

    with pytest.raises(RuntimeError, match="phase write failed"):
        prepared.mark_succeeded()

    state = read_json_object(prepared.state_path)
    publication = state["ref_publication"]
    assert isinstance(publication, dict)
    assert publication["phase"] == "prepared"
    destinations = publication["destinations"]
    assert isinstance(destinations, dict)
    for destination in destinations.values():
        assert isinstance(destination, dict)
        assert _ref_oid(repo, str(destination["name"])) is None
    source = prepared.worktree_capture.source
    remove_worktree_workspace(
        source, prepared.workspace_path, prepared.worktree_capture.git_dir
    )


@pytest.mark.parametrize(
    ("publication", "message"),
    (
        ({"destinations": {}}, "destinations are incomplete"),
        (
            {"destinations": {"candidate": None, "result": None}},
            "destination is invalid",
        ),
        (
            {
                "destinations": {
                    "candidate": {
                        "name": "refs/test/candidate",
                        "target_oid": "bad",
                        "expected_old_oid": None,
                    },
                    "result": None,
                }
            },
            "destination is invalid",
        ),
        (
            {
                "destinations": {
                    "candidate": {
                        "name": "refs/test/candidate",
                        "target_oid": "a" * 40,
                        "expected_old_oid": "bad",
                    },
                    "result": None,
                }
            },
            "expected OID is invalid",
        ),
    ),
)
def test_publication_destinations_reject_malformed_evidence(
    publication: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        ref_publication.publication_destinations({"ref_publication": publication})


def _prepared_lineage_workspace(tmp_path: Path) -> tuple[Path, PreparedWorkspace]:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=False,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    (prepared.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    assert prepared.worktree_capture is not None
    return repo, prepared


def _published_lineage_workspace(
    tmp_path: Path,
) -> tuple[Path, PreparedWorkspace, dict[str, object]]:
    repo, prepared = _prepared_lineage_workspace(tmp_path)
    prepared.mark_succeeded()
    assert prepared.state_path is not None
    return repo, prepared, read_json_object(prepared.state_path)


def _remove_published_workspace(
    repo: Path,
    prepared: PreparedWorkspace,
    state: dict[str, object],
) -> None:
    refs = state["refs"]
    assert isinstance(refs, dict)
    run_git_text(repo, "update-ref", "-d", str(refs["candidate"]))
    run_git_text(repo, "update-ref", "-d", str(refs["result"]))
    assert prepared.workspace_path is not None
    assert prepared.worktree_capture is not None
    remove_worktree_workspace(
        prepared.worktree_capture.source,
        prepared.workspace_path,
        prepared.worktree_capture.git_dir,
    )


def _ref_oid(repo: Path, ref_name: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", repo.as_posix(), "rev-parse", "--verify", ref_name],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _repository_id(repo: Path) -> str:
    object_format = run_git_text(repo, "rev-parse", "--show-object-format=storage")
    return workspace_repository_id(repo / ".git", repo, object_format)


def _write_dedicated_ref_evidence(
    evidence_path: Path,
    repository_id: str,
    run_key_name: str,
    node_id: str,
    task_id: str,
    ref_name: str,
    target_oid: str,
) -> None:
    evidence_path.write_text(
        json.dumps(
            {
                "evidence_kind": "temporary_ref_cleanup",
                "run_id": "run-id",
                "run_key_name": run_key_name,
                "node_id": node_id,
                "task_id": task_id,
                "role": "artifact_consumer",
                "round_num": 0,
                "audit_round_num": None,
                "git": {"repo_id": repository_id},
                "temporary_refs": [
                    {
                        "phase": "prepared",
                        "name": ref_name,
                        "target_oid": target_oid,
                        "owner_run_id": "run-id",
                        "owner_node_id": node_id,
                        "owner_task_id": task_id,
                        "owner_role": "artifact_consumer",
                        "owner_round_num": 0,
                        "owner_audit_round_num": None,
                        "repository_id": repository_id,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import crewplane.runtime.workspace.worktree.orchestration as worktree_orchestration
import crewplane.runtime.workspace.worktree.ref_publication as ref_publication
from crewplane.runtime.workspace.worktree import (
    remove_worktree_workspace,
)
from crewplane.runtime.workspace.worktree.protected_refs import (
    protected_ref_snapshot_for_scopes,
)
from tests.helpers.workspace_service import (
    read_json_object,
    run_git_text,
)
from tests.unit.runtime.workspace.ref_publication_support import (
    prepared_lineage_workspace,
    published_lineage_workspace,
    ref_oid,
    remove_published_workspace,
)


def test_result_refs_are_published_in_one_transaction_after_prepared_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, prepared = prepared_lineage_workspace(tmp_path)
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
    remove_published_workspace(repo, prepared, state)


def test_result_ref_transaction_atomically_verifies_consumed_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, prepared = prepared_lineage_workspace(tmp_path)
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
        assert ref_oid(repo, str(destination["name"])) is None
    assert ref_oid(repo, consumed_ref) == moved_oid
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
    repo, prepared, state = published_lineage_workspace(tmp_path)
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
    assert ref_oid(repo, str(refs["candidate"])) is None
    assert ref_oid(repo, str(refs["result"])) is None
    source = prepared.worktree_capture.source
    remove_worktree_workspace(
        source, prepared.workspace_path, prepared.worktree_capture.git_dir
    )


def test_result_ref_cleanup_leaves_moved_ref_and_preserves_publication_phase(
    tmp_path: Path,
) -> None:
    repo, prepared, state = published_lineage_workspace(tmp_path)
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

    assert ref_oid(repo, candidate_ref) == source.run_base_commit
    assert ref_oid(repo, result_ref) is None
    assert read_json_object(prepared.state_path)["ref_publication"]["phase"] == (
        "published"
    )
    run_git_text(repo, "update-ref", "-d", candidate_ref)
    remove_worktree_workspace(
        source, prepared.workspace_path, prepared.worktree_capture.git_dir
    )


@pytest.mark.parametrize("target_exists", [False, True], ids=("dangling", "live"))
def test_result_ref_cleanup_rejects_symbolic_ref_without_touching_target(
    tmp_path: Path,
    target_exists: bool,
) -> None:
    repo, prepared, state = published_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    assert prepared.workspace_path is not None
    refs = state["refs"]
    assert isinstance(refs, dict)
    candidate_ref = str(refs["candidate"])
    result_ref = str(refs["result"])
    target_ref = "refs/heads/user-work"
    target_oid = ref_oid(repo, candidate_ref)
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
    assert ref_oid(repo, target_ref) == (target_oid if target_exists else None)
    assert ref_oid(repo, result_ref) is not None
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
    repo, prepared, state = published_lineage_workspace(tmp_path)
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
    assert ref_oid(repo, str(refs["candidate"])) is not None
    assert ref_oid(repo, str(refs["result"])) is not None
    assert read_json_object(prepared.state_path)["ref_publication"]["phase"] == (
        "published"
    )
    remove_published_workspace(repo, prepared, state)


def test_result_ref_cleanup_treats_already_absent_refs_as_removed(
    tmp_path: Path,
) -> None:
    repo, prepared, state = published_lineage_workspace(tmp_path)
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
    repo, prepared = prepared_lineage_workspace(tmp_path)
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
    assert ref_oid(repo, str(destinations["result"]["name"])) is None
    run_git_text(repo, "update-ref", "-d", injected_ref)
    source = prepared.worktree_capture.source
    remove_worktree_workspace(
        source, prepared.workspace_path, prepared.worktree_capture.git_dir
    )


@pytest.mark.parametrize("target_exists", [False, True], ids=("dangling", "live"))
def test_result_ref_publication_rejects_symbolic_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_exists: bool,
) -> None:
    repo, prepared = prepared_lineage_workspace(tmp_path)
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
    assert ref_oid(repo, target_ref) == (target_oid if target_exists else None)
    publication = read_json_object(prepared.state_path)["ref_publication"]
    destinations = publication["destinations"]
    assert isinstance(destinations, dict)
    assert ref_oid(repo, str(destinations["result"]["name"])) is None
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
    repo, prepared = prepared_lineage_workspace(tmp_path)
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
        assert ref_oid(repo, str(destination["name"])) is None
    source = prepared.worktree_capture.source
    remove_worktree_workspace(
        source, prepared.workspace_path, prepared.worktree_capture.git_dir
    )


@pytest.mark.parametrize(
    ("publication", "message"),
    [
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
    ],
)
def test_publication_destinations_reject_malformed_evidence(
    publication: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        ref_publication.publication_destinations({"ref_publication": publication})


@pytest.mark.parametrize(
    ("invalid_fields", "message"),
    [
        ({"node_id": ""}, "Workspace ref publication lacks node_id identity."),
        ({"task_id": None}, "Workspace ref publication lacks task_id identity."),
        ({"round_num": True}, "Workspace ref publication lacks round identity."),
        (
            {"audit_round_num": False},
            "Workspace ref publication has invalid audit identity.",
        ),
        (
            {"node_id": "", "task_id": "", "round_num": True, "audit_round_num": False},
            "Workspace ref publication lacks node_id identity.",
        ),
        (
            {"task_id": "", "round_num": True, "audit_round_num": False},
            "Workspace ref publication lacks task_id identity.",
        ),
        (
            {"round_num": True, "audit_round_num": False},
            "Workspace ref publication lacks round identity.",
        ),
    ],
)
def test_reconciliation_rejects_invalid_invocation_before_ref_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_fields, message: str
) -> None:
    repo, prepared, state = published_lineage_workspace(tmp_path)
    assert prepared.state_path is not None
    refs_before = run_git_text(
        repo, "for-each-ref", "--format=%(refname) %(objectname)"
    )
    state.update(invalid_fields)
    prepared.state_path.write_text(json.dumps(state))
    state_before = prepared.state_path.read_bytes()
    mutations = []
    real_git = ref_publication.git

    class TrackingCommand:
        def __init__(self, cwd: Path) -> None:
            self._command = real_git(cwd)

        def text(self, *args: str) -> str:
            return self._command.text(*args)

        def run_with_input(self, stdin: bytes, *args: str):
            mutations.append(stdin)
            return self._command.run_with_input(stdin, *args)

    monkeypatch.setattr(ref_publication, "git", TrackingCommand)
    with pytest.raises(RuntimeError) as exc_info:
        ref_publication.reconcile_result_ref_publication(
            prepared.state_path, repo, repo / ".git"
        )

    assert str(exc_info.value) == message
    assert mutations == []
    assert prepared.state_path.read_bytes() == state_before
    assert (
        run_git_text(repo, "for-each-ref", "--format=%(refname) %(objectname)")
        == refs_before
    )
    remove_published_workspace(repo, prepared, state)

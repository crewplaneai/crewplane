from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import UUID

import pytest

from crewplane.core.workspace.invocation_identity import invocation_slug
from crewplane.core.workspace.repository_identity import workspace_repository_id
from crewplane.runtime.workspace.state import (
    discard_workspace_lineage,
    read_workspace_state,
)
from crewplane.runtime.workspace.worktree import (
    remove_worktree_workspace,
    temporary_refs,
)
from crewplane.runtime.workspace.worktree.ref_cleanup import delete_run_workspace_refs
from crewplane.runtime.workspace.worktree.temporary_refs import TemporaryRefOwner
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    run_git_text,
    workspace_plan,
)
from tests.unit.runtime.workspace.ref_publication_support import (
    published_lineage_workspace,
    ref_oid,
    remove_published_workspace,
)


@pytest.mark.parametrize(
    "invalid_identity",
    [
        None,
        ("node_id", ""),
        ("task_id", None),
        ("round_num", True),
        ("audit_round_num", False),
    ],
)
def test_external_ref_cleanup_consumes_dedicated_temporary_ref_evidence(
    tmp_path: Path,
    invalid_identity: tuple[str, object] | None,
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

    if invalid_identity is not None:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        field, value = invalid_identity
        payload[field] = value
        evidence_path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(RuntimeError, match="lacks invocation identity"):
            temporary_refs.reconcile_temporary_import_refs(
                evidence_path, repo, repo / ".git", repository_id
            )
        assert ref_oid(repo, ref_name) == target_oid
        assert evidence_path.exists()
        return

    removed = delete_run_workspace_refs(
        repo,
        repo / ".git",
        repo,
        run_key_name,
        run_dir,
    )

    assert removed == 1
    assert ref_oid(repo, ref_name) is None
    assert not evidence_path.exists()


def test_external_ref_cleanup_consumes_empty_prepared_temporary_ref_evidence(
    monkeypatch,
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
    monkeypatch.setattr(temporary_refs, "uuid4", lambda: UUID(int=1))
    owner = TemporaryRefOwner.dedicated(
        plan,
        source,
        evidence_dir,
        "consumer",
        "workspace-file-readme",
    )
    assert (
        owner.state_path.name
        == "workspace-temporary-refs-00000000000000000000000000000001.json"
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

    assert ref_oid(repo, ref_name) == target_oid
    assert evidence_path.exists()


def test_external_ref_cleanup_reconciles_nested_planned_stage_state(
    tmp_path: Path,
) -> None:
    repo, prepared, state = published_lineage_workspace(tmp_path)
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
    assert ref_oid(repo, str(refs["candidate"])) is None
    assert ref_oid(repo, str(refs["result"])) is None
    remove_worktree_workspace(
        prepared.worktree_capture.source,
        prepared.workspace_path,
        prepared.worktree_capture.git_dir,
    )


@pytest.mark.parametrize("stage_path", [".", "./"])
def test_ref_cleanup_rejects_dot_stage_before_mutating_refs(
    tmp_path: Path, stage_path: str
) -> None:
    repo, prepared, state = published_lineage_workspace(tmp_path)
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
    assert ref_oid(repo, str(refs["candidate"])) is not None
    assert ref_oid(repo, str(refs["result"])) is not None
    remove_published_workspace(repo, prepared, state)


def test_external_ref_cleanup_rejects_unresolved_finalizer_before_mutation(
    tmp_path: Path,
) -> None:
    repo, prepared, state = published_lineage_workspace(tmp_path)
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

    assert ref_oid(repo, str(refs["candidate"])) is not None
    assert ref_oid(repo, str(refs["result"])) is not None
    payload.pop("workspace_mutator")
    prepared.state_path.write_text(json.dumps(payload), encoding="utf-8")
    remove_published_workspace(repo, prepared, state)


def test_external_ref_cleanup_accepts_discarded_lineage_publication(
    tmp_path: Path,
) -> None:
    repo, prepared, state = published_lineage_workspace(tmp_path)
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
    assert ref_oid(repo, str(refs["candidate"])) is None
    assert ref_oid(repo, str(refs["result"])) is None
    publication = read_workspace_state(prepared.state_path)["ref_publication"]
    assert isinstance(publication, dict)
    assert publication["phase"] == "removed"
    remove_published_workspace(repo, prepared, state)


def test_external_ref_cleanup_rejects_contradictory_discarded_publication(
    tmp_path: Path,
) -> None:
    repo, prepared, state = published_lineage_workspace(tmp_path)
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

    assert ref_oid(repo, str(refs["candidate"])) is not None
    assert ref_oid(repo, str(refs["result"])) is not None
    remove_published_workspace(repo, prepared, state)


@pytest.mark.parametrize("unsafe_kind", ["malformed_archive", "symlinked_state"])
def test_external_ref_cleanup_prevalidates_all_claims_before_mutation(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    repo, prepared, state = published_lineage_workspace(tmp_path)
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

    assert ref_oid(repo, str(refs["candidate"])) is not None
    assert ref_oid(repo, str(refs["result"])) is not None
    unsafe_path.unlink()
    remove_published_workspace(repo, prepared, state)


def test_external_ref_cleanup_rejects_symlinked_claim_directory_before_mutation(
    tmp_path: Path,
) -> None:
    repo, prepared, state = published_lineage_workspace(tmp_path)
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

    assert ref_oid(repo, str(refs["candidate"])) is not None
    assert ref_oid(repo, str(refs["result"])) is not None
    (run_dir / "other-node").unlink()
    remove_published_workspace(repo, prepared, state)


def test_external_ref_cleanup_consumes_archived_reuse_claim(tmp_path: Path) -> None:
    repo, prepared, state = published_lineage_workspace(tmp_path)
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
    assert ref_oid(repo, str(refs["candidate"])) is None
    assert ref_oid(repo, str(refs["result"])) is None
    source = prepared.worktree_capture.source
    remove_worktree_workspace(
        source, prepared.workspace_path, prepared.worktree_capture.git_dir
    )


def test_external_ref_cleanup_ignores_non_authoritative_workspace_like_metadata(
    tmp_path: Path,
) -> None:
    repo, prepared, state = published_lineage_workspace(tmp_path)
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
    assert ref_oid(repo, str(refs["candidate"])) is None
    assert ref_oid(repo, str(refs["result"])) is None
    remove_worktree_workspace(
        prepared.worktree_capture.source,
        prepared.workspace_path,
        prepared.worktree_capture.git_dir,
    )


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

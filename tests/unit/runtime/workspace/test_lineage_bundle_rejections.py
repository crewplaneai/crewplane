from __future__ import annotations

import hashlib
import shutil
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from crewplane.artifacts.workspace.chain_validation import (
    verify_persisted_workspace_result_chain,
    verify_workspace_source_chain,
)
from crewplane.runtime.workspace.worktree import (
    WorktreeSourceRef,
    create_worktree_workspace,
)
from crewplane.runtime.workspace.worktree.lineage import (
    verify_source_commit_available,
)
from crewplane.runtime.workspace.worktree.types import (
    WorkspaceSourceKind,
)
from tests.helpers.workspace_lineage_bundles import (
    create_full_bundle_chain,
    create_prerequisite_bundle_chain,
    create_pruned_result_bundle,
    create_result_bundle,
)
from tests.helpers.workspace_service import (
    create_git_repo,
    git_commit_exists,
    run_git_text,
    workspace_plan,
)
from tests.unit.runtime.workspace.service_worktree_lineage_bundles_support import (
    project_source_ref,
)


def test_verify_source_commit_available_rejects_ambient_omitted_upstream(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    first, second = create_prerequisite_bundle_chain(
        repo,
        tmp_path / "first.bundle",
        tmp_path / "second.bundle",
    )
    ambient_first_ref = "refs/crewplane/test/ambient-first"
    ambient_second_ref = "refs/crewplane/test/ambient-second"
    run_git_text(
        repo, "fetch", first.path.as_posix(), f"{first.ref}:{ambient_first_ref}"
    )
    run_git_text(
        repo,
        "fetch",
        second.path.as_posix(),
        f"{second.ref}:{ambient_second_ref}",
    )
    run_git_text(repo, "update-ref", "-d", ambient_first_ref)
    run_git_text(repo, "update-ref", "-d", ambient_second_ref)
    assert git_commit_exists(repo, first.commit)
    assert git_commit_exists(repo, second.commit)
    source_refs_before = run_git_text(
        repo,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
    )

    with pytest.raises(RuntimeError) as exc_info:
        verify_source_commit_available(
            source,
            WorktreeSourceRef(
                source_kind="node",
                source_node_id="second",
                source_commit=second.commit,
                source_tree=second.tree,
                candidate_sequence=1,
                bundle_path=second.path,
                bundle_sha256=second.sha256,
                bundle_size_bytes=second.size_bytes,
                bundle_ref=second.ref,
            ),
        )

    message = str(exc_info.value)
    assert message.startswith(
        "Workspace lineage source verification failed while validating recorded "
        "Git artifacts:"
    )
    assert "Git did not provide diagnostic output" not in message
    assert "crewplane-lineage-verify-" not in message
    assert "Command '['git'" not in message
    assert (
        run_git_text(repo, "for-each-ref", "--format=%(refname) %(objectname)")
        == source_refs_before
    )


def test_verify_source_commit_available_rejects_ambient_project_commit(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    (repo / "ambient.txt").write_text("ambient\n", encoding="utf-8")
    run_git_text(repo, "add", "ambient.txt")
    run_git_text(repo, "commit", "-m", "ambient")
    ambient_commit = run_git_text(repo, "rev-parse", "HEAD^{commit}")
    ambient_tree = run_git_text(repo, "rev-parse", "HEAD^{tree}")
    source_refs_before = run_git_text(
        repo,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
    )

    with pytest.raises(
        RuntimeError, match="project source descriptor is contradictory"
    ):
        verify_source_commit_available(
            source,
            WorktreeSourceRef(
                source_kind="project",
                source_node_id=None,
                source_commit=ambient_commit,
                source_tree=ambient_tree,
            ),
        )

    assert (
        run_git_text(repo, "for-each-ref", "--format=%(refname) %(objectname)")
        == source_refs_before
    )


def test_worktree_workspace_rejects_imported_source_tree_mismatch(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    result_commit, tree, result_ref, bundle_path, bundle_sha256 = (
        create_pruned_result_bundle(tmp_path, repo)
    )
    if git_commit_exists(repo, result_commit):
        pytest.skip("git retained the test commit after pruning")
    wrong_tree = "f" * 40
    assert wrong_tree != tree

    with pytest.raises(RuntimeError, match="result tree mismatch"):
        create_worktree_workspace(
            plan,
            "bad-imported-source-tree",
            source,
            WorktreeSourceRef(
                source_kind="node",
                source_node_id="upstream",
                source_commit=result_commit,
                source_tree=wrong_tree,
                candidate_sequence=1,
                bundle_path=bundle_path,
                bundle_sha256=bundle_sha256,
                bundle_size_bytes=bundle_path.stat().st_size,
                bundle_ref=result_ref,
                upstream_sources=(project_source_ref(source),),
            ),
        )


def test_worktree_workspace_rejects_missing_local_source_bundle(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    result_commit, tree, result_ref, bundle_path, bundle_sha256 = create_result_bundle(
        tmp_path, repo, "missing-local-source"
    )
    assert git_commit_exists(repo, result_commit)
    bundle_size = bundle_path.stat().st_size
    bundle_path.unlink()
    workspace_path = (
        cache_root
        / "workspaces"
        / source.repository_id
        / plan.run_key_name
        / "missing-local-source-bundle"
    )

    with pytest.raises(RuntimeError, match="bundle is missing"):
        create_worktree_workspace(
            plan,
            "missing-local-source-bundle",
            source,
            WorktreeSourceRef(
                source_kind="node",
                source_node_id="upstream",
                source_commit=result_commit,
                source_tree=tree,
                candidate_sequence=1,
                bundle_path=bundle_path,
                bundle_sha256=bundle_sha256,
                bundle_size_bytes=bundle_size,
                bundle_ref=result_ref,
            ),
        )

    assert not workspace_path.exists()


def test_worktree_workspace_rejects_tampered_local_source_bundle(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    result_commit, tree, result_ref, bundle_path, bundle_sha256 = create_result_bundle(
        tmp_path, repo, "tampered-local-source"
    )
    assert git_commit_exists(repo, result_commit)
    bundle_size = bundle_path.stat().st_size
    tampered = bytearray(bundle_path.read_bytes())
    tampered[-1] ^= 1
    bundle_path.write_bytes(tampered)

    with pytest.raises(RuntimeError, match="bundle digest mismatch"):
        create_worktree_workspace(
            plan,
            "tampered-local-source-bundle",
            source,
            WorktreeSourceRef(
                source_kind="node",
                source_node_id="upstream",
                source_commit=result_commit,
                source_tree=tree,
                candidate_sequence=1,
                bundle_path=bundle_path,
                bundle_sha256=bundle_sha256,
                bundle_size_bytes=bundle_size,
                bundle_ref=result_ref,
            ),
        )


def test_worktree_workspace_rejects_local_source_bundle_ref_mismatch(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    result_commit, tree, _result_ref, bundle_path, bundle_sha256 = create_result_bundle(
        tmp_path, repo, "wrong-ref-local-source"
    )
    assert git_commit_exists(repo, result_commit)

    with pytest.raises(RuntimeError, match="bundle ref or target OID mismatch"):
        create_worktree_workspace(
            plan,
            "wrong-ref-local-source-bundle",
            source,
            WorktreeSourceRef(
                source_kind="node",
                source_node_id="upstream",
                source_commit=result_commit,
                source_tree=tree,
                candidate_sequence=1,
                bundle_path=bundle_path,
                bundle_sha256=bundle_sha256,
                bundle_size_bytes=bundle_path.stat().st_size,
                bundle_ref="refs/crewplane/test/missing",
            ),
        )


def test_worktree_workspace_rejects_candidate_source_without_bundle(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None

    with pytest.raises(RuntimeError, match="bundle descriptor is incomplete"):
        create_worktree_workspace(
            plan,
            "candidate-missing-bundle",
            source,
            WorktreeSourceRef(
                source_kind="candidate",
                source_node_id="implement",
                source_commit=source.run_base_commit,
                source_tree=source.source_tree,
                candidate_sequence=2,
            ),
        )


def test_isolated_chain_verifier_rejects_descriptor_shape_and_bundle_claims(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    result_commit, tree, result_ref, bundle_path, bundle_sha256 = create_result_bundle(
        tmp_path, repo, "validation-errors"
    )
    project = project_source_ref(source)
    valid = WorktreeSourceRef(
        source_kind="node",
        source_node_id="upstream",
        source_commit=result_commit,
        source_tree=tree,
        candidate_sequence=1,
        bundle_path=bundle_path,
        bundle_sha256=bundle_sha256,
        bundle_size_bytes=bundle_path.stat().st_size,
        bundle_ref=result_ref,
        upstream_sources=(project,),
    )

    with pytest.raises(RuntimeError, match="bundle kind is invalid"):
        verify_workspace_source_chain(
            source,
            replace(valid, source_kind=cast(WorkspaceSourceKind, "invalid")),
        )
    with pytest.raises(RuntimeError, match="lacks a source node"):
        verify_workspace_source_chain(source, replace(valid, source_node_id=None))
    with pytest.raises(RuntimeError, match="exactly one parent source"):
        verify_workspace_source_chain(source, replace(valid, upstream_sources=()))
    with pytest.raises(RuntimeError, match="exactly one parent source"):
        verify_workspace_source_chain(
            source,
            replace(valid, upstream_sources=(project, project)),
        )
    with pytest.raises(RuntimeError, match="bundle size mismatch"):
        verify_workspace_source_chain(
            source,
            replace(valid, bundle_size_bytes=bundle_path.stat().st_size + 1),
        )

    bundle_link = tmp_path / "linked.bundle"
    bundle_link.symlink_to(bundle_path)
    with pytest.raises(RuntimeError, match="must be a regular file"):
        verify_workspace_source_chain(source, replace(valid, bundle_path=bundle_link))

    invalid_header = tmp_path / "incomplete.bundle"
    invalid_header.write_bytes(b"# v2 git bundle\n")
    with pytest.raises(RuntimeError, match="header is incomplete"):
        verify_workspace_source_chain(
            source,
            replace(
                valid,
                bundle_path=invalid_header,
                bundle_sha256=hashlib.sha256(invalid_header.read_bytes()).hexdigest(),
                bundle_size_bytes=invalid_header.stat().st_size,
            ),
        )

    invalid_source = source.model_copy(update={"object_format": "invalid"})
    with pytest.raises(RuntimeError, match="Unsupported workspace object format"):
        verify_workspace_source_chain(invalid_source, project)


def test_isolated_chain_verifier_rejects_multiple_refs_and_wrong_parent(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    result_commit, tree, result_ref, _bundle_path, _bundle_sha256 = (
        create_result_bundle(tmp_path, repo, "multiple-refs")
    )
    extra_ref = "refs/crewplane/test/extra"
    run_git_text(repo, "update-ref", extra_ref, source.run_base_commit)
    multiple_bundle = tmp_path / "multiple.bundle"
    run_git_text(
        repo,
        "bundle",
        "create",
        multiple_bundle.as_posix(),
        result_ref,
        extra_ref,
    )
    with pytest.raises(RuntimeError, match="exactly one ref"):
        verify_workspace_source_chain(
            source,
            WorktreeSourceRef(
                source_kind="node",
                source_node_id="upstream",
                source_commit=result_commit,
                source_tree=tree,
                bundle_path=multiple_bundle,
                bundle_sha256=hashlib.sha256(multiple_bundle.read_bytes()).hexdigest(),
                bundle_size_bytes=multiple_bundle.stat().st_size,
                bundle_ref=result_ref,
                upstream_sources=(project_source_ref(source),),
            ),
        )

    first, second = create_full_bundle_chain(
        repo,
        tmp_path / "first-parent.bundle",
        tmp_path / "second-parent.bundle",
    )
    with pytest.raises(RuntimeError, match="unexpected parent"):
        verify_workspace_source_chain(
            source,
            WorktreeSourceRef(
                source_kind="node",
                source_node_id="second",
                source_commit=second.commit,
                source_tree=second.tree,
                bundle_path=second.path,
                bundle_sha256=second.sha256,
                bundle_size_bytes=second.size_bytes,
                bundle_ref=second.ref,
                upstream_sources=(project_source_ref(source),),
            ),
        )
    assert first.commit != source.run_base_commit


def test_persisted_chain_verifier_rejects_unsafe_descriptor_fields(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "descriptor.bundle").write_bytes(b"descriptor")
    payload: dict[str, object] = {
        "node_id": "upstream",
        "source": {
            "kind": "project",
            "node_id": None,
            "commit": source.run_base_commit,
            "tree": source.source_tree,
            "upstream_sources": [],
        },
        "result": {
            "result_commit": "c" * 40,
            "result_tree": "d" * 40,
        },
        "refs": {"result": "refs/crewplane/test/result"},
        "bundle": {
            "path": "descriptor.bundle",
            "sha256": "e" * 64,
            "size_bytes": 42,
        },
    }

    source_payload = payload["source"]
    assert isinstance(source_payload, dict)
    source_payload["upstream_sources"] = "invalid"
    with pytest.raises(RuntimeError, match="descriptor chain is invalid"):
        verify_persisted_workspace_result_chain(source, run_dir, payload)

    source_payload["upstream_sources"] = []
    bundle = payload["bundle"]
    assert isinstance(bundle, dict)
    bundle["path"] = "/absolute.bundle"
    with pytest.raises(RuntimeError, match="bundle path is unsafe"):
        verify_persisted_workspace_result_chain(source, run_dir, payload)

    bundle["path"] = "missing.bundle"
    with pytest.raises(RuntimeError, match="bundle path is missing"):
        verify_persisted_workspace_result_chain(source, run_dir, payload)

    payload["node_id"] = ""
    with pytest.raises(RuntimeError, match="lacks a required string"):
        verify_persisted_workspace_result_chain(source, run_dir, payload)

    payload["node_id"] = "upstream"
    bundle["path"] = "descriptor.bundle"
    bundle["size_bytes"] = True
    with pytest.raises(RuntimeError, match="lacks a required size"):
        verify_persisted_workspace_result_chain(source, run_dir, payload)

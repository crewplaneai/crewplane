from __future__ import annotations

import hashlib
import json

from orchestrator_cli.artifacts.resume_validation import validate_resume_frontier
from orchestrator_cli.artifacts.workspace_rendered_file_validation import (
    provider_rendered_workspace_files_match,
)
from orchestrator_cli.core.preflight.models import WorkspaceFileLocator
from tests.helpers.resume import (
    attach_workspace_descriptor,
    make_node_state,
    make_plan,
    write_node_state,
    write_result,
)
from tests.helpers.resume_validation import (
    attach_git_workspace_source,
    attach_source_bundle_descriptor,
    provider_workspace_state_payload,
    run_git_text,
    source_record,
    write_lineage_bundle_for_payload,
)
from tests.helpers.workspace_records import workspace_selection_record


def test_validate_frontier_accepts_matching_rendered_workspace_file_descriptors(
    tmp_path,
) -> None:
    source, plan, payload = _source_with_rendered_workspace_file_descriptor(tmp_path)
    (source.run_dir / "a" / "workspace-state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    attach_workspace_descriptor(source.run_dir, plan, "a")

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ("a",)


def test_validate_frontier_rejects_mismatched_rendered_workspace_file_digest(
    tmp_path,
) -> None:
    source, plan, payload = _source_with_rendered_workspace_file_descriptor(tmp_path)
    rendered = payload["rendered_workspace_files"]
    assert isinstance(rendered, list)
    rendered[0]["injected_sha256"] = "f" * 64
    (source.run_dir / "a" / "workspace-state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    attach_workspace_descriptor(source.run_dir, plan, "a")

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_rejects_mismatched_rendered_workspace_file_invocation(
    tmp_path,
) -> None:
    source, plan, payload = _source_with_rendered_workspace_file_descriptor(tmp_path)
    rendered = payload["rendered_workspace_files"]
    assert isinstance(rendered, list)
    rendered[0]["invocation_id"] = "a.executor.alpha.round-2"
    (source.run_dir / "a" / "workspace-state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    attach_workspace_descriptor(source.run_dir, plan, "a")

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def test_rendered_workspace_file_accepts_dynamic_source_bundle(
    tmp_path,
) -> None:
    source, plan, payload = _source_with_dynamic_rendered_workspace_file_descriptor(
        tmp_path,
    )

    assert provider_rendered_workspace_files_match(plan, plan.nodes[0], payload, source)


def test_rendered_workspace_file_rejects_dynamic_source_blob_mismatch(
    tmp_path,
) -> None:
    source, plan, payload = _source_with_dynamic_rendered_workspace_file_descriptor(
        tmp_path,
    )
    rendered = payload["rendered_workspace_files"]
    assert isinstance(rendered, list)
    rendered[0]["git_blob"] = "f" * 40

    assert not provider_rendered_workspace_files_match(
        plan,
        plan.nodes[0],
        payload,
        source,
    )


def _source_with_rendered_workspace_file_descriptor(tmp_path):
    source = source_record(tmp_path)
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    locator = _workspace_file_locator()
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    plan = plan.model_copy(
        update={
            "nodes": [node, plan.nodes[1]],
            "workspace_file_locators": [locator],
        }
    )
    plan, repo = attach_git_workspace_source(tmp_path, plan)
    assert plan.workspace_source is not None
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )
    payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
    )
    payload["rendered_workspace_files"] = [
        _rendered_workspace_file_descriptor(plan, locator)
    ]
    write_lineage_bundle_for_payload(repo, source, payload)
    return source, plan, payload


def _source_with_dynamic_rendered_workspace_file_descriptor(tmp_path):
    source = source_record(tmp_path)
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
        source_kind="node",
        source_node_id="upstream",
    )
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    plan = plan.model_copy(update={"nodes": [node, plan.nodes[1]]})
    plan, repo = attach_git_workspace_source(tmp_path, plan)
    assert plan.workspace_source is not None
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )
    upstream_payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
        node_id="upstream",
    )
    write_lineage_bundle_for_payload(repo, source, upstream_payload)
    upstream_result = upstream_payload["result"]
    assert isinstance(upstream_result, dict)
    locator = _dynamic_workspace_file_locator(repo)
    payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=str(upstream_result["result_commit"]),
        source_tree=str(upstream_result["result_tree"]),
        source_kind="node",
        source_node_id="upstream",
        candidate_sequence=1,
    )
    attach_source_bundle_descriptor(payload, upstream_payload)
    payload["rendered_workspace_files"] = [
        _rendered_workspace_file_descriptor(
            plan,
            locator,
            "node",
            "upstream",
            1,
            str(upstream_result["result_commit"]),
            str(upstream_result["result_tree"]),
        )
    ]
    plan = plan.model_copy(update={"workspace_file_locators": [locator]})
    return source, plan, payload


def _workspace_file_locator() -> WorkspaceFileLocator:
    payload = b"workspace prompt\n"
    return WorkspaceFileLocator(
        locator_id="workspace-file-rendered",
        content_ref="workspace-files/workspace-file-rendered.txt",
        occurrence_id="a:executor:0:file:README.md",
        node_id="a",
        target="executor_prompt",
        source_class="project_initial",
        raw_token="{{file:README.md}}",
        raw_path="README.md",
        source_root="/repo",
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="README.md",
        workspace_relative_path="README.md",
        git_blob="e" * 40,
        git_file_mode="100644",
        byte_size=len(payload),
        canonical_blob_sha256=hashlib.sha256(payload).hexdigest(),
        literal_path_verified=True,
        utf8_validated=True,
    )


def _dynamic_workspace_file_locator(repo) -> WorkspaceFileLocator:
    payload = (repo / "README.md").read_bytes()
    blob = run_git_text(repo, "rev-parse", "HEAD:README.md")
    return WorkspaceFileLocator(
        locator_id="workspace-file-dynamic",
        content_ref=None,
        occurrence_id="a:executor:0:file:README.md",
        node_id="a",
        target="executor_prompt",
        source_class="runtime_dynamic",
        raw_token="{{file:README.md}}",
        raw_path="README.md",
        source_root=repo.as_posix(),
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="README.md",
        workspace_relative_path="README.md",
        git_blob=None,
        git_file_mode=None,
        byte_size=None,
        canonical_blob_sha256=None,
        literal_path_verified=False,
        utf8_validated=False,
    ).model_copy(
        update={
            "git_blob": blob,
            "git_file_mode": "100644",
            "byte_size": len(payload),
            "canonical_blob_sha256": hashlib.sha256(payload).hexdigest(),
            "literal_path_verified": True,
            "utf8_validated": True,
        }
    )


def _rendered_workspace_file_descriptor(
    plan,
    locator: WorkspaceFileLocator,
    source_kind: str = "project",
    source_node_id: str | None = None,
    candidate_sequence: int | None = None,
    source_commit: str | None = None,
    source_tree: str | None = None,
) -> dict[str, object]:
    source = plan.workspace_source
    assert source is not None
    return {
        "occurrence_id": locator.occurrence_id,
        "invocation_id": "a.executor.alpha.round-1",
        "role": "executor",
        "round_num": 1,
        "audit_round_num": None,
        "source_kind": source_kind,
        "source_node_id": source_node_id,
        "source_commit": source_commit or source.run_base_commit,
        "source_tree": source_tree or source.source_tree,
        "candidate_sequence": candidate_sequence,
        "workspace_relative_path": locator.workspace_relative_path,
        "git_blob": locator.git_blob,
        "git_file_mode": locator.git_file_mode,
        "byte_size": locator.byte_size,
        "canonical_blob_sha256": locator.canonical_blob_sha256,
        "injected_sha256": locator.canonical_blob_sha256,
        "byte_source": "git_blob",
        "literal_path_verified": True,
        "utf8_validated": True,
        "target": locator.target,
    }

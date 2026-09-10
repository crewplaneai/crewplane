from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from crewplane.artifacts.manager import OutputManager
from crewplane.artifacts.resume.hydration import hydrate_resume_frontier
from crewplane.artifacts.resume.validation import validate_resume_frontier
from crewplane.artifacts.run_history import find_same_context_runs
from crewplane.artifacts.workspace.rendered_file_validation import (
    provider_rendered_workspace_files_match,
)
from crewplane.core.preflight.models import PreflightExecutionPlan, WorkspaceFileLocator
from crewplane.runtime.execution.workflow.cleanup import (
    cleanup_successful_workspace_run_refs,
)
from crewplane.runtime.execution.workspace_files import resolve_workspace_file
from crewplane.runtime.workspace.worktree.descriptors import load_source_ref_from_state
from tests.helpers.resume import (
    WORKFLOW_IDENTITY,
    WORKFLOW_NAME,
    WORKFLOW_SIGNATURE,
    attach_workspace_descriptor,
    make_node_state,
    make_plan,
    make_run_manifest,
    replace_plan_fields,
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


@pytest.mark.parametrize(
    "corruption",
    [
        "missing-run",
        "missing-workspace-source",
        "mismatched-injected-digest",
        "source-commit-type",
        "source-tree-type",
        "missing-bundle-path",
        "missing-bundle-file",
        "bundle-digest-type",
        "bundle-size-type",
        "bundle-size-mismatch",
        "bundle-hash-mismatch",
        "unreadable-bundle",
    ],
)
def test_dynamic_rendered_files_require_verifiable_source_bundle_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    source, plan, payload = _source_with_dynamic_rendered_workspace_file_descriptor(
        tmp_path
    )
    assert provider_rendered_workspace_files_match(plan, plan.nodes[0], payload, source)
    descriptor = payload["rendered_workspace_files"][0]
    invocation_source = payload["invocation_source"]
    bundle = source.run_dir / invocation_source["source_bundle_path"]
    original_open = Path.open

    def open_file(path: Path, *args: object, **kwargs: object) -> object:
        if path == bundle:
            raise OSError("bundle became unreadable")
        return original_open(path, *args, **kwargs)

    if corruption == "missing-run":
        source = None
    elif corruption == "missing-workspace-source":
        fields = plan.model_dump(mode="json")
        fields["workspace_source"] = None
        plan = PreflightExecutionPlan.model_validate(fields)
    elif corruption == "mismatched-injected-digest":
        descriptor["injected_sha256"] = "f" * 64
    elif corruption in {"source-commit-type", "source-tree-type"}:
        field = "source_commit" if corruption == "source-commit-type" else "source_tree"
        descriptor[field] = None
        invocation_source[field] = None
    elif corruption == "missing-bundle-path":
        invocation_source["source_bundle_path"] = None
    elif corruption == "missing-bundle-file":
        bundle.unlink()
    elif corruption == "bundle-digest-type":
        invocation_source["source_bundle_sha256"] = None
    elif corruption == "bundle-size-type":
        invocation_source["source_bundle_size_bytes"] = True
    elif corruption == "bundle-size-mismatch":
        invocation_source["source_bundle_size_bytes"] += 1
    elif corruption == "bundle-hash-mismatch":
        invocation_source["source_bundle_sha256"] = "f" * 64
    else:
        monkeypatch.setattr(Path, "open", open_file)

    assert not provider_rendered_workspace_files_match(
        plan, plan.nodes[0], payload, source
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("git_blob", "f" * 40),
        ("git_file_mode", "100755"),
        ("byte_size", 999),
        ("audit_round_num", True),
    ],
)
def test_project_rendered_files_must_match_the_compiled_locator(
    tmp_path: Path, field: str, value: object
) -> None:
    source, plan, payload = _source_with_rendered_workspace_file_descriptor(tmp_path)
    assert provider_rendered_workspace_files_match(plan, plan.nodes[0], payload, source)
    payload["rendered_workspace_files"][0][field] = value

    assert not provider_rendered_workspace_files_match(
        plan, plan.nodes[0], payload, source
    )


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


@pytest.mark.parametrize(
    "resume_count", [1, 2, 3], ids=["once", "twice", "three-times"]
)
def test_hydrated_lineage_survives_source_rendering_and_resume_consumers(
    tmp_path: Path,
    resume_count: int,
) -> None:
    source, plan, payload = _source_with_rendered_workspace_file_descriptor(tmp_path)
    assert plan.workspace_source is not None
    repo = plan.workspace_source.git_top_level
    locator = plan.workspace_file_locators[0]
    source_state_path = source.run_dir / "a" / "workspace-state.json"
    source_state_path.parent.mkdir(parents=True, exist_ok=True)
    source_state_path.write_text(json.dumps(payload), encoding="utf-8")
    attach_workspace_descriptor(source.run_dir, plan, "a")
    state_dir = Path(repo) / ".crewplane"
    prior_payload = payload
    run_keys = {source.manifest.run_key_name}

    for generation in range(1, resume_count + 1):
        frontier = validate_resume_frontier(source, plan)
        output = OutputManager("Workflow", base_dir=state_dir)
        output.write_run_manifest(
            make_run_manifest(
                output.run_id,
                output.run_key_name,
                status="running",
                started_offset=generation,
            )
        )

        assert hydrate_resume_frontier(frontier, plan, output) == ("a",)

        hydrated_state_path = output.stages_dir / "a" / "workspace-state.json"
        hydrated_payload = json.loads(hydrated_state_path.read_text(encoding="utf-8"))
        assert output.run_key_name not in run_keys
        run_keys.add(output.run_key_name)
        assert hydrated_payload["run_id"] == output.run_id
        assert hydrated_payload["run_key_name"] == output.run_key_name
        origin = hydrated_payload["resume_origin"]
        assert origin["source_run_id"] == source.manifest.run_id
        assert origin["source_run_key_name"] == source.manifest.run_key_name
        assert origin["source_workspace"] == prior_payload["workspace"]
        assert origin["source_execution"] == prior_payload["execution"]
        assert origin.get("source_resume_origin") == prior_payload.get("resume_origin")
        assert hydrated_payload["workspace"]["retained_reason"] == "hydrated_resume"
        assert all(
            hydrated_payload["workspace"][field] is None
            for field in (
                "path",
                "effective_cwd",
                "cache_root",
                "checkout_root",
                "cache_key",
            )
        )
        assert all(value is None for value in hydrated_payload["execution"].values())
        assert "ref_publication" not in hydrated_payload
        assert "temporary_refs" not in hydrated_payload
        assert (output.results_dir / "a-result.md").read_text("utf-8") == "a output"
        source_bundle = source.run_dir / str(prior_payload["bundle"]["path"])
        hydrated_bundle = output.stages_dir / hydrated_payload["bundle"]["path"]
        assert hydrated_bundle.read_bytes() == source_bundle.read_bytes()
        source_ref = load_source_ref_from_state(hydrated_state_path)
        assert source_ref.source_commit == payload["result"]["result_commit"]
        assert provider_rendered_workspace_files_match(
            plan, plan.nodes[0], hydrated_payload
        )
        resolved = resolve_workspace_file(
            plan,
            output,
            locator.locator_id,
            workspace_candidate_source=True,
        )
        assert resolved.text == "ready\n"
        fields = plan.model_dump(mode="json")
        fields.update(run_id=output.run_id, run_key_name=output.run_key_name)
        target_plan = PreflightExecutionPlan.model_validate(fields)
        assert (
            asyncio.run(cleanup_successful_workspace_run_refs(target_plan, None)) == 0
        )
        output.write_run_manifest(
            make_run_manifest(
                output.run_id,
                output.run_key_name,
                status="failed",
                started_offset=generation,
            )
        )
        source = next(
            record
            for record in find_same_context_runs(
                state_dir,
                WORKFLOW_IDENTITY,
                WORKFLOW_NAME,
                WORKFLOW_SIGNATURE,
            )
            if record.manifest.run_key_name == output.run_key_name
        )
        prior_payload = hydrated_payload
        assert validate_resume_frontier(source, plan).resumed_node_ids == ("a",)


def _source_with_rendered_workspace_file_descriptor(tmp_path):
    source = source_record(tmp_path)
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    plan, repo = attach_git_workspace_source(tmp_path, plan)
    locator = _workspace_file_locator(repo)
    fields = plan.model_dump(mode="json")
    replace_plan_fields(
        fields, {"nodes/0/workspace_policy": policy.model_dump(mode="json")}
    )
    plan = _with_workspace_locator(fields, locator)
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
    plan, repo = attach_git_workspace_source(tmp_path, plan)
    fields = plan.model_dump(mode="json")
    upstream = plan.nodes[0].model_dump(mode="json")
    upstream.update(
        id="upstream",
        render_plan_id="upstream",
        workspace_policy=workspace_selection_record().model_dump(mode="json"),
        artifact_contract={
            "stage_path": "upstream",
            "output_path": "upstream-result.md",
            "log_path": "upstream/logs",
            "result_path": "upstream-result.md",
        },
    )
    fields["nodes"].append(upstream)
    fields["execution_order"].insert(0, "upstream")
    fields["render_plans"].append({"render_plan_id": "upstream", "node_id": "upstream"})
    edge = plan.dependency_graph[0].model_dump(mode="json")
    edge.update(
        source_node="upstream", target_node="a", target_locator="upstream.output"
    )
    fields["dependency_graph"].append(edge)
    replace_plan_fields(
        fields,
        {
            "nodes/0/workspace_policy": policy.model_dump(mode="json"),
            "nodes/0/dependencies": ["upstream"],
        },
    )
    locator = _dynamic_workspace_file_locator(repo)
    plan = _with_workspace_locator(fields, locator)
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
    rendered = payload["rendered_workspace_files"][0]
    content = (repo / "README.md").read_bytes()
    rendered.update(
        git_blob=run_git_text(repo, "rev-parse", "HEAD:README.md"),
        git_file_mode="100644",
        byte_size=len(content),
        canonical_blob_sha256=hashlib.sha256(content).hexdigest(),
        injected_sha256=hashlib.sha256(content).hexdigest(),
    )
    return source, plan, payload


def _with_workspace_locator(
    fields: dict[str, Any], locator: WorkspaceFileLocator
) -> PreflightExecutionPlan:
    fields["workspace_file_locators"] = [locator.model_dump(mode="json")]
    fields["render_plans"][0]["streams"] = [
        {
            "target_role": "executor",
            "fragments": [
                {
                    "fragment_index": 0,
                    "kind": "workspace_file_locator",
                    "source_role": "executor",
                    "locator": {"locator_id": locator.locator_id},
                }
            ],
        }
    ]
    return PreflightExecutionPlan.model_validate(fields)


def _workspace_file_locator(repo: Path) -> WorkspaceFileLocator:
    payload = (repo / "README.md").read_bytes()
    return WorkspaceFileLocator(
        locator_id="workspace-file-rendered",
        content_ref="workspace-files/workspace-file-rendered.txt",
        occurrence_id="a:executor:0:file:README.md",
        node_id="a",
        target="executor_prompt",
        source_class="project_initial_then_candidate",
        raw_token="{{file:README.md}}",
        raw_path="README.md",
        source_root=repo.as_posix(),
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="README.md",
        workspace_relative_path="README.md",
        git_blob=run_git_text(repo, "rev-parse", "HEAD:README.md"),
        git_file_mode="100644",
        byte_size=len(payload),
        canonical_blob_sha256=hashlib.sha256(payload).hexdigest(),
        literal_path_verified=True,
        utf8_validated=True,
    )


def _dynamic_workspace_file_locator(repo: Path) -> WorkspaceFileLocator:
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

from __future__ import annotations

import json

from orchestrator_cli.artifacts.naming import build_generated_file_result_dir_name
from orchestrator_cli.artifacts.resume_validation import validate_resume_frontier
from orchestrator_cli.core.execution_state import ArtifactDescriptor
from orchestrator_cli.core.preflight.models import WorkspaceBranchExportRecord
from orchestrator_cli.version import SCHEMA_VERSION
from tests.helpers.resume import (
    attach_workspace_descriptor,
    make_node_state,
    make_plan,
    sha256_hex,
    write_node_state,
    write_result,
)
from tests.helpers.resume_validation import (
    attach_git_workspace_source,
    provider_workspace_state_payload,
    source_record,
    write_lineage_bundle_for_payload,
)
from tests.helpers.workspace_records import workspace_selection_record


def test_validate_frontier_accepts_dependency_closed_node_state(tmp_path) -> None:
    source = source_record(tmp_path)
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )

    frontier = validate_resume_frontier(source, make_plan())

    assert frontier.resumed_node_ids == ("a",)


def test_validate_frontier_accepts_provider_workspace_state_with_bundle(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    plan = plan.model_copy(
        update={
            "nodes": [node, plan.nodes[1]],
        }
    )
    plan, repo = attach_git_workspace_source(tmp_path, plan)
    assert plan.workspace_source is not None
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir, make_node_state(source.manifest, "a", [descriptor])
    )
    payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
    )
    write_lineage_bundle_for_payload(repo, source, payload)
    state_path = source.run_dir / "a" / "workspace-state.json"
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    attach_workspace_descriptor(source.run_dir, plan, "a")

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ("a",)


def test_validate_frontier_accepts_branch_only_workspace_changes(tmp_path) -> None:
    source = source_record(tmp_path)
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    plan = plan.model_copy(
        update={
            "nodes": [node, plan.nodes[1]],
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
    write_lineage_bundle_for_payload(repo, source, payload)
    state_path = source.run_dir / "a" / "workspace-state.json"
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    attach_workspace_descriptor(source.run_dir, plan, "a")
    payload["branch_export"] = {
        "status": "created",
        "operation": "created",
        "branch_name": "ai/old-branch",
        "branch_ref": "refs/heads/ai/old-branch",
        "record_artifact": "workspace-exports/primary.json",
        "result_commit": "b" * 40,
        "result_tree": "d" * 40,
        "completed_at": "2026-06-16T12:00:00",
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    current_policy = policy.model_copy(
        update={
            "branch_export": WorkspaceBranchExportRecord(
                create_branch=True,
                branch_name="ai/new-branch",
            )
        }
    )
    current_plan = plan.model_copy(
        update={
            "nodes": [
                node.model_copy(update={"workspace_policy": current_policy}),
                plan.nodes[1],
            ],
        }
    )

    frontier = validate_resume_frontier(source, current_plan)

    assert frontier.resumed_node_ids == ("a",)


def test_validate_frontier_rejects_provider_workspace_missing_invoker_descriptor(
    tmp_path,
) -> None:
    def remove_invoker(payload):
        payload.pop("invoker", None)

    frontier = _provider_workspace_frontier(tmp_path, remove_invoker)

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_rejects_provider_workspace_mismatched_invoker_descriptor(
    tmp_path,
) -> None:
    def change_invoker(payload):
        invoker = payload["invoker"]
        assert isinstance(invoker, dict)
        invoker["launch_mode"] = "different-launch-mode"

    frontier = _provider_workspace_frontier(tmp_path, change_invoker)

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_accepts_applied_controlled_child_environment(
    tmp_path,
) -> None:
    def mark_child_environment_applied(payload):
        payload["child_process_environment"] = {"required": True, "applied": True}

    frontier = _provider_workspace_frontier(
        tmp_path,
        mark_child_environment_applied,
        runtime_config_snapshot=_runtime_command_runner_snapshot(),
    )

    assert frontier.resumed_node_ids == ("a",)


def test_validate_frontier_rejects_unapplied_controlled_child_environment(
    tmp_path,
) -> None:
    def mark_child_environment_unapplied(payload):
        payload["child_process_environment"] = {"required": True, "applied": False}

    frontier = _provider_workspace_frontier(
        tmp_path,
        mark_child_environment_unapplied,
        runtime_config_snapshot=_runtime_command_runner_snapshot(),
    )

    assert frontier.resumed_node_ids == ()


def test_invalid_upstream_invalidates_descendant(tmp_path) -> None:
    source = source_record(tmp_path)
    bad_descriptor = write_result(source.results_dir, "a-result.md", "a output")
    bad_descriptor = bad_descriptor.model_copy(update={"sha256": "0" * 64})
    b_descriptor = write_result(source.results_dir, "b-result.md", "b output")
    write_node_state(
        source.run_dir, make_node_state(source.manifest, "a", [bad_descriptor])
    )
    write_node_state(
        source.run_dir, make_node_state(source.manifest, "b", [b_descriptor])
    )

    frontier = validate_resume_frontier(source, make_plan())

    assert frontier.resumed_node_ids == ()


def test_node_state_missing_schema_marker_is_not_reusable(tmp_path) -> None:
    source = source_record(tmp_path)
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    node_state = make_node_state(source.manifest, "a", [descriptor])
    node_state_path = write_node_state(source.run_dir, node_state)
    payload = node_state.model_dump(mode="json", exclude_none=True)
    del payload["run_state_schema_version"]
    node_state_path.write_text(json.dumps(payload), encoding="utf-8")

    frontier = validate_resume_frontier(source, make_plan())

    assert frontier.resumed_node_ids == ()


def test_required_findings_come_from_dependency_graph(tmp_path) -> None:
    source = source_record(tmp_path)
    output_descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [output_descriptor]),
    )

    missing_findings = validate_resume_frontier(source, make_plan(findings_edge=True))

    assert missing_findings.resumed_node_ids == ()

    findings_descriptor = write_result(
        source.results_dir,
        "a-findings.md",
        "findings",
    )
    write_node_state(
        source.run_dir,
        make_node_state(
            source.manifest,
            "a",
            [output_descriptor, findings_descriptor],
        ),
    )

    with_findings = validate_resume_frontier(source, make_plan(findings_edge=True))

    assert with_findings.resumed_node_ids == ("a",)


def test_validate_frontier_rejects_missing_generated_file_sidecar(tmp_path) -> None:
    source = source_record(tmp_path)
    output_descriptor = write_result(source.results_dir, "a-result.md", "a output")
    generated_descriptor = ArtifactDescriptor(
        kind="generated_file",
        relative_path="generated-files/a/alpha/src/app.txt",
        sha256=sha256_hex("generated"),
        size_bytes=len(b"generated"),
    )
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [output_descriptor]).model_copy(
            update={"generated_files": [generated_descriptor]}
        ),
    )

    frontier = validate_resume_frontier(source, make_plan())

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_rejects_generated_file_outside_namespace(tmp_path) -> None:
    source = source_record(tmp_path)
    output_descriptor = write_result(source.results_dir, "a-result.md", "a output")
    source_file = source.results_dir / "a-extra.md"
    source_file.write_text("generated", encoding="utf-8")
    generated_descriptor = ArtifactDescriptor(
        kind="generated_file",
        relative_path="a-extra.md",
        sha256=sha256_hex("generated"),
        size_bytes=len(b"generated"),
    )
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [output_descriptor]).model_copy(
            update={"generated_files": [generated_descriptor]}
        ),
    )

    frontier = validate_resume_frontier(source, make_plan())

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_rejects_generated_file_for_different_node(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    output_descriptor = write_result(source.results_dir, "a-result.md", "a output")
    generated_path = source.results_dir / "generated-files/b/alpha/src/app.txt"
    generated_path.parent.mkdir(parents=True)
    generated_path.write_text("generated", encoding="utf-8")
    generated_descriptor = ArtifactDescriptor(
        kind="generated_file",
        relative_path="generated-files/b/alpha/src/app.txt",
        sha256=sha256_hex("generated"),
        size_bytes=len(b"generated"),
    )
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [output_descriptor]).model_copy(
            update={"generated_files": [generated_descriptor]}
        ),
    )

    frontier = validate_resume_frontier(source, make_plan())

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_accepts_generated_file_for_bounded_node_directory(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    node_id = "build." + ("x" * 150)
    node_dir = build_generated_file_result_dir_name(node_id)
    output_descriptor = write_result(source.results_dir, "a-result.md", "a output")
    generated_path = source.results_dir / "generated-files" / node_dir / "alpha/app.txt"
    generated_path.parent.mkdir(parents=True)
    generated_path.write_text("generated", encoding="utf-8")
    generated_descriptor = ArtifactDescriptor(
        kind="generated_file",
        relative_path=generated_path.relative_to(source.results_dir).as_posix(),
        sha256=sha256_hex("generated"),
        size_bytes=len(b"generated"),
    )
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, node_id, [output_descriptor]).model_copy(
            update={"generated_files": [generated_descriptor]}
        ),
    )

    frontier = validate_resume_frontier(source, _single_node_plan(node_id))

    assert frontier.resumed_node_ids == (node_id,)


def _provider_workspace_frontier(
    tmp_path,
    payload_mutator,
    runtime_config_snapshot: dict[str, object] | None = None,
):
    source = source_record(tmp_path)
    plan = make_plan()
    if runtime_config_snapshot is not None:
        plan = plan.model_copy(
            update={"runtime_config_snapshot": runtime_config_snapshot}
        )
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
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
    payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
    )
    write_lineage_bundle_for_payload(repo, source, payload)
    payload_mutator(payload)
    state_path = source.run_dir / "a" / "workspace-state.json"
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    attach_workspace_descriptor(source.run_dir, plan, "a")
    return validate_resume_frontier(source, plan)


def _runtime_command_runner_snapshot() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "invoker": {
            "implementation": "cli",
            "capabilities": {
                "workspace": {
                    "honors_cwd": True,
                    "launch_mode": "runtime_command_runner",
                    "controlled_child_environment": True,
                }
            },
        },
    }


def _single_node_plan(node_id: str):
    plan = make_plan()
    node = plan.nodes[0].model_copy(
        update={
            "id": node_id,
            "dependencies": [],
        }
    )
    return plan.model_copy(
        update={
            "execution_order": [node_id],
            "nodes": [node],
            "dependency_graph": [],
        }
    )

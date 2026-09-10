from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from crewplane.artifacts.resume.validation import validate_resume_frontier
from crewplane.cli.app import app
from crewplane.cli.workspace_cleanup.context import cleanup_repository_id
from crewplane.core.workspace.invocation_identity import invocation_slug
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import (
    attach_workspace_descriptor,
    make_node_state,
    make_plan,
    write_node_state,
    write_result,
)
from tests.helpers.resume_validation import (
    attach_git_workspace_source,
    snapshot_workspace_state_payload,
    source_record,
)
from tests.helpers.workspace_records import (
    workspace_selection_record,
)
from tests.unit.cli.cleanup_support import (
    cleanup_project,
    run_cleanup_git,
)


@pytest.mark.parametrize("moved_temporary_ref", [False, True])
def test_cleanup_workspaces_preserves_resume_frontier(
    tmp_path: Path,
    moved_temporary_ref: bool,
) -> None:
    plan, project_root = attach_git_workspace_source(tmp_path, make_plan())
    repository_id_value = cleanup_repository_id(project_root, all_projects=False)
    assert repository_id_value is not None
    assert plan.workspace_source is not None
    workspace_policy = workspace_selection_record(enabled=True, kind="snapshot")
    plan = plan.model_copy(
        update={
            "workspace_source": plan.workspace_source.model_copy(
                update={"repository_id": repository_id_value}
            ),
            "nodes": [
                plan.nodes[0].model_copy(update={"workspace_policy": workspace_policy}),
                plan.nodes[1],
            ],
        }
    )
    state_dir = project_root / ".crewplane"
    source = source_record(state_dir, status="failed")
    plan = plan.model_copy(
        update={
            "run_id": source.manifest.run_id,
            "run_key_name": source.manifest.run_key_name,
            "project_root": project_root.as_posix(),
            "context_root": source.run_dir.as_posix(),
            "manifest_root": (source.run_dir / "manifests").as_posix(),
        }
    )
    plan_path = source.run_dir / source.manifest.preflight_plan_path
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    result_descriptor = write_result(
        source.results_dir,
        "a-result.md",
        "a output",
    )
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [result_descriptor]),
    )
    cache_root = tmp_path / "cache"
    workspace_path = (
        cache_root
        / "snapshots"
        / repository_id_value
        / source.manifest.run_key_name
        / invocation_slug("a", "alpha", None, 1)
    )
    checkout_root = workspace_path / "checkout"
    checkout_root.mkdir(parents=True)
    state_payload = snapshot_workspace_state_payload(source, plan, "alpha")
    workspace_payload = state_payload["workspace"]
    assert isinstance(workspace_payload, dict)
    workspace_payload["cache_key"] = workspace_path.name
    state_payload["execution"] = {
        "cache_root": cache_root.as_posix(),
        "workspace_path": workspace_path.as_posix(),
        "checkout_root": checkout_root.as_posix(),
        "effective_cwd": checkout_root.as_posix(),
        "worktree_git_dir": None,
    }
    if moved_temporary_ref:
        assert plan.workspace_source is not None
        slug = invocation_slug("a", "alpha", None, 1)
        temporary_ref = f"refs/crewplane/runs/workflow--source/imports/a/{slug}/moved"
        state_payload["temporary_refs"] = [
            {
                "phase": "prepared",
                "name": temporary_ref,
                "target_oid": plan.workspace_source.run_base_commit,
                "owner_run_id": source.manifest.run_id,
                "owner_node_id": "a",
                "owner_task_id": "alpha",
                "owner_role": "executor",
                "owner_round_num": 1,
                "owner_audit_round_num": None,
                "repository_id": repository_id_value,
            }
        ]
        (project_root / "moved.txt").write_text("moved\n", encoding="utf-8")
        run_cleanup_git(project_root, "add", "moved.txt")
        run_cleanup_git(project_root, "commit", "-m", "move temporary ref")
        run_cleanup_git(project_root, "update-ref", temporary_ref, "HEAD")
    state_path = source.run_dir / "a" / "workspace-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state_payload), encoding="utf-8")
    attach_workspace_descriptor(source.run_dir, plan, "a")
    config_path = state_dir / "config.yml"
    config_path.write_text(
        "\n".join(
            [
                f'version: "{SCHEMA_VERSION}"',
                "agents:",
                "  alpha:",
                '    cli_cmd: ["mock"]',
                '    default_model: "test"',
                "settings:",
                "  workspace:",
                "    enabled: true",
                f'    cache_root: "{cache_root.as_posix()}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    assert validate_resume_frontier(source, plan).resumed_node_ids == ("a",)

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    if moved_temporary_ref:
        assert result.exit_code == 1
        assert "Workspace temporary import ref moved and was retained" in result.output
    else:
        assert result.exit_code == 0, result.output
    assert not workspace_path.exists(), result.output
    assert (
        json.loads(state_path.read_text(encoding="utf-8"))["workspace"]["retention"]
        == "deleted"
    )
    assert validate_resume_frontier(source, plan).resumed_node_ids == ("a",)


def test_cleanup_workspaces_reconciles_hydrated_run_temporary_ref_evidence(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    state_path = (
        project_root / ".crewplane/execution-stages/run-1/node/workspace-state.json"
    )
    nested_state_path = (
        state_path.parents[1] / "custom/build-stage/workspace-state.json"
    )
    nested_state_path.parent.mkdir(parents=True)
    state_path.replace(nested_state_path)
    state_path = nested_state_path
    plan_path = state_path.parents[2] / "preflight/execution-plan.json"
    plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    nested_contract = plan_payload["nodes"][0]["artifact_contract"]
    nested_contract["stage_path"] = "custom/build-stage"
    nested_contract["output_path"] = "custom/build-stage/output.md"
    nested_contract["log_path"] = "custom/build-stage/logs"
    nested_contract["result_path"] = "custom/build-stage/output.md"
    plan_path.write_text(json.dumps(plan_payload), encoding="utf-8")
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    workspace = payload["workspace"]
    execution = payload["execution"]
    payload["resume_origin"] = {
        "source_run_id": "source-run",
        "source_run_key_name": "source-run-key",
        "source_node_id": "node",
        "hydrated_at": "2026-08-30T00:00:00+00:00",
        "source_workspace": dict(workspace),
        "source_execution": dict(execution),
    }
    for field in ("path", "effective_cwd", "cache_root", "checkout_root", "cache_key"):
        workspace[field] = None
    workspace["retention"] = "not_applicable"
    workspace["retained_reason"] = "hydrated_resume"
    for field in (
        "cache_root",
        "workspace_path",
        "checkout_root",
        "effective_cwd",
        "worktree_git_dir",
    ):
        execution[field] = None
    payload.pop("ref_publication")
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    run_cleanup_git(
        project_root,
        "worktree",
        "remove",
        "--force",
        (workspace_path / "checkout").as_posix(),
    )
    workspace_path.rmdir()

    target_oid = run_cleanup_git(project_root, "rev-parse", "HEAD^{commit}").strip()
    slug = invocation_slug("node", "branch-export-primary", None, 0)
    temporary_ref = f"refs/crewplane/runs/run-1/imports/node/{slug}/temporary"
    run_cleanup_git(project_root, "update-ref", temporary_ref, target_oid)
    evidence_path = state_path.with_name("workspace-temporary-refs-interrupted.json")
    evidence_path.write_text(
        json.dumps(
            {
                "evidence_kind": "temporary_ref_cleanup",
                "run_id": "run-1",
                "run_key_name": "run-1",
                "node_id": "node",
                "task_id": "branch-export-primary",
                "role": "artifact_consumer",
                "round_num": 0,
                "audit_round_num": None,
                "git": {"repo_id": payload["git"]["repo_id"]},
                "temporary_refs": [
                    {
                        "phase": "prepared",
                        "name": temporary_ref,
                        "target_oid": target_oid,
                        "owner_run_id": "run-1",
                        "owner_node_id": "node",
                        "owner_task_id": "branch-export-primary",
                        "owner_role": "artifact_consumer",
                        "owner_round_num": 0,
                        "owner_audit_round_num": None,
                        "repository_id": payload["git"]["repo_id"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "Removed 1 run-owned Git ref(s)." in result.output
    assert run_cleanup_git(project_root, "for-each-ref", temporary_ref).strip() == ""
    assert not evidence_path.exists()

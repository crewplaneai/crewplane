from __future__ import annotations

import json
import subprocess
from pathlib import Path

from rich.console import Console

import crewplane.cli.cleanup as cleanup_cli
from crewplane.cli.workspace_cleanup.context import cleanup_repository_id
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.workspace.invocation_identity import invocation_slug
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import (
    make_plan,
    make_run_manifest,
    write_run_manifest,
)
from tests.helpers.workspace_records import (
    WORKTREE_CONTRACT_PAYLOAD,
    workspace_selection_record,
)


def resolve_workspace_cleanup_context(
    config_path: Path,
    orphans: bool = False,
) -> cleanup_cli.WorkspaceCleanupContext:
    return cleanup_cli.resolve_cleanup_workspace_context(
        console=Console(),
        config_file=config_path,
        successful=False,
        failed=False,
        cancelled=False,
        all_projects=False,
        run_key_name=None,
        older_than=None,
        orphans=orphans,
    )


def cleanup_project(
    tmp_path: Path,
    initialize_git: bool = False,
    create_workspace: bool = True,
    run_status: str = "succeeded",
) -> tuple[Path, Path, Path]:
    project_root = tmp_path / "project"
    state_dir = project_root / ".crewplane"
    cache_root = tmp_path / "workspace-cache"
    project_root.mkdir()
    if initialize_git:
        run_cleanup_git(project_root, "init")
        run_cleanup_git(project_root, "config", "user.name", "Crewplane Test")
        run_cleanup_git(
            project_root, "config", "user.email", "crewplane-test@example.invalid"
        )
        (project_root / "README.md").write_text("ready\n", encoding="utf-8")
        run_cleanup_git(project_root, "add", ".")
        run_cleanup_git(project_root, "commit", "-m", "initial")
    repo_id = (
        cleanup_repository_id(project_root, all_projects=False)
        if initialize_git
        else "repo-1"
    )
    workspace_path = (
        cache_root
        / "workspaces"
        / repo_id
        / "run-1"
        / invocation_slug("node", "alpha", None, 1)
    )
    if create_workspace:
        if initialize_git:
            workspace_path.parent.mkdir(parents=True)
            checkout_root = workspace_path / "checkout"
            run_cleanup_git(
                project_root,
                "worktree",
                "add",
                "--detach",
                checkout_root.as_posix(),
                "HEAD",
            )
            (checkout_root / "file.txt").write_text("payload", encoding="utf-8")
        else:
            workspace_path.mkdir(parents=True)
            (workspace_path / "file.txt").write_text("payload", encoding="utf-8")
    state_dir.mkdir(parents=True)
    if create_workspace:
        state_path = (
            state_dir / "execution-stages" / "run-1" / "node" / "workspace-state.json"
        )
        state_path.parent.mkdir(parents=True)
        state_payload = (
            cleanup_workspace_state(
                project_root,
                workspace_path,
                repo_id,
                run_status,
            )
            if initialize_git
            else {
                "run_key_name": "run-1",
                "status": run_status,
                "workspace": {"cache_key": workspace_path.name},
            }
        )
        state_path.write_text(
            json.dumps(state_payload),
            encoding="utf-8",
        )
        write_cleanup_plan_and_manifest(
            state_dir,
            project_root,
            run_status,
        )
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
                "    cleanup_on_success: true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return project_root, config_path, workspace_path


def write_cleanup_plan_and_manifest(
    state_dir: Path,
    project_root: Path,
    run_status: str,
) -> None:
    manifest = make_run_manifest(
        run_id="run-1",
        run_key_name="run-1",
        status=run_status,
    )
    run_dir = state_dir / "execution-stages" / "run-1"
    payload = make_plan().model_dump(mode="json")
    payload.update(
        {
            "run_id": manifest.run_id,
            "run_key_name": manifest.run_key_name,
            "project_root": project_root.as_posix(),
            "context_root": run_dir.as_posix(),
            "manifest_root": (run_dir / "manifests").as_posix(),
            "execution_order": ["node"],
            "nodes": [payload["nodes"][0]],
            "render_plans": [payload["render_plans"][0]],
            "dependency_graph": [],
        }
    )
    node = payload["nodes"][0]
    assert isinstance(node, dict)
    node["id"] = "node"
    node["render_plan_id"] = "node"
    node["dependencies"] = []
    node["workspace_policy"] = workspace_selection_record(
        kind="worktree",
        logical_name="primary",
        lineage_producer=True,
    ).model_dump(mode="json")
    contract = node["artifact_contract"]
    assert isinstance(contract, dict)
    contract.update(
        {
            "stage_path": "node",
            "output_path": "node/output.md",
            "findings_path": None,
            "log_path": "node/logs",
            "result_path": "node/output.md",
        }
    )
    render_plan = payload["render_plans"][0]
    assert isinstance(render_plan, dict)
    render_plan["render_plan_id"] = "node"
    render_plan["node_id"] = "node"
    plan = PreflightExecutionPlan.model_validate(payload)
    plan_path = run_dir / manifest.preflight_plan_path
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(plan.model_dump_json(), encoding="utf-8")
    write_run_manifest(state_dir, manifest)


def cleanup_workspace_state(
    project_root: Path,
    workspace_path: Path,
    repo_id: str,
    status: str,
) -> dict[str, object]:
    manifest = make_run_manifest(
        run_id="run-1",
        run_key_name="run-1",
        status=status,
    )
    head = run_cleanup_git(project_root, "rev-parse", "HEAD^{commit}").strip()
    tree = run_cleanup_git(project_root, "rev-parse", "HEAD^{tree}").strip()
    checkout_root = workspace_path / "checkout"
    git_dir_text = run_cleanup_git(checkout_root, "rev-parse", "--git-dir").strip()
    git_dir_path = Path(git_dir_text)
    git_dir = (
        git_dir_path if git_dir_path.is_absolute() else checkout_root / git_dir_path
    ).resolve()
    common_git_dir = (
        project_root
        / run_cleanup_git(project_root, "rev-parse", "--git-common-dir").strip()
    ).resolve()
    slug = invocation_slug("node", "alpha", None, 1)
    ref_root = f"refs/crewplane/runs/run-1/node/{slug}"
    candidate_ref = f"{ref_root}/candidate"
    result_ref = f"{ref_root}/result"
    return {
        "version": SCHEMA_VERSION,
        "run_id": "run-1",
        "run_key_name": "run-1",
        "workflow_name": manifest.workflow_name,
        "workflow_signature": manifest.workflow_signature,
        "node_id": "node",
        "task_id": "alpha",
        "provider": "alpha",
        "role": "executor",
        "round_num": 1,
        "audit_round_num": None,
        "status": status,
        "workspace_kind": "worktree",
        "logical_worktree_name": "primary",
        "clean_start": "strict",
        "worktree_contract": WORKTREE_CONTRACT_PAYLOAD,
        "git": {
            "object_format": run_cleanup_git(
                project_root, "rev-parse", "--show-object-format=storage"
            ).strip(),
            "repo_id": repo_id,
            "run_base_commit": head,
            "source_tree": tree,
            "git_top_level": project_root.as_posix(),
            "active_git_dir": git_dir.as_posix(),
            "common_git_dir": common_git_dir.as_posix(),
        },
        "source": {
            "kind": "project",
            "node_id": None,
            "commit": head,
            "tree": tree,
            "candidate_sequence": None,
        },
        "invocation_source": {
            "source_kind": "project",
            "source_node_id": None,
            "source_commit": head,
            "source_tree": tree,
            "candidate_sequence": None,
        },
        "workspace": {
            "path": workspace_path.as_posix(),
            "effective_cwd": checkout_root.as_posix(),
            "materialization": "worktree_checkout",
            "writable": True,
            "lineage_producer": True,
            "retention": "retained",
            "retained_reason": None,
            "project_root_relative_path": ".",
            "reuse_generation": 1,
            "cache_key": workspace_path.name,
        },
        "execution": {
            "cache_root": workspace_path.parents[3].as_posix(),
            "workspace_path": workspace_path.as_posix(),
            "checkout_root": checkout_root.as_posix(),
            "effective_cwd": checkout_root.as_posix(),
            "worktree_git_dir": git_dir.as_posix(),
        },
        "process_drain": {"status": "confirmed"},
        "result": {
            "candidate_commit": head,
            "result_commit": head,
            "candidate_tree": tree,
            "result_tree": tree,
            "changed_path_count": 0,
            "final_head": head,
        },
        "bundle": {
            "path": f"node/workspace-bundles/{slug}.bundle",
            "sha256": "f" * 64,
            "size_bytes": 0,
            "verified": True,
        },
        "refs": {"candidate": candidate_ref, "result": result_ref},
        "ref_publication": {
            "phase": "removed",
            "repository_id": repo_id,
            "run_id": "run-1",
            "run_key_name": "run-1",
            "node_id": "node",
            "task_id": "alpha",
            "role": "executor",
            "round_num": 1,
            "audit_round_num": None,
            "destinations": {
                "candidate": {
                    "name": candidate_ref,
                    "target_oid": head,
                    "expected_old_oid": None,
                },
                "result": {
                    "name": result_ref,
                    "target_oid": head,
                    "expected_old_oid": None,
                },
            },
        },
    }


def run_cleanup_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo.as_posix(), *args],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8", errors="replace")

import asyncio
import io
import json
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from crewplane.artifacts.expansions import reconstruct_expansions
from crewplane.cli.app import app
from crewplane.cli.workflow_runner import execute_workflow_run
from crewplane.cli.workspace_cleanup.evidence_claims import load_authoritative_plan
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.preflight.source import load_workflow_source_for_preflight
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.foreach import (
    compile_foreach_file,
    compile_literal_foreach,
    workflow_document,
    write_workflow_document,
)
from tests.helpers.isolated_git import IsolatedGit
from tests.helpers.platforms import requires_workspace_support
from tests.helpers.workspace_workflow_fixtures import write_fixture
from tests.helpers.workspace_workflow_runner import workspace_config, workspace_project

isolated_git = isolated_git_support.isolated_git
pytestmark = requires_workspace_support


def test_generated_workspaces_lineage_exports_capture_and_history(
    tmp_path: Path, isolated_git: IsolatedGit, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace_project(tmp_path, isolated_git)
    compile_literal_foreach(root)
    child = root / "child.md"
    payload, body = workflow_document(child)
    payload["worktrees"] = {"change": {"kind": "worktree", "create_branch": True}}
    payload["nodes"] = payload["nodes"][:2] + [
        {
            "id": "after",
            "mode": "parallel",
            "needs": ["analyze"],
            "providers": ["alpha"],
        }
    ]
    write_workflow_document(
        child,
        payload,
        body.split("## independent")[0]
        + "\n{{file:src/app.txt}}\n## after\nRead {{file:src/app.txt}} and {{analyze.output}}.",
    )
    (root / ".gitignore").write_bytes(b".crewplane/\n")
    isolated_git.run_text(root, "add", ".")
    isolated_git.run_text(root, "commit", "-m", "foreach workflow")
    config = workspace_config(tmp_path / "cache", tmp_path / "fixtures")
    config.settings.max_concurrent_nodes = 2
    for ordinal in range(2):
        write_fixture(
            tmp_path / "fixtures",
            f"chunks.i{ordinal}.analyze",
            "alpha_executor_0_round1.md",
            "Updated `src/app.txt`.\n",
            sidecar={
                "required_prompt_contains": ["base application"],
                "workspace_mutations": [
                    {"path": "src/app.txt", "content": f"item {ordinal} edit\n"}
                ],
            },
        )
        write_fixture(
            tmp_path / "fixtures",
            f"chunks.i{ordinal}.after",
            "alpha_executor_0_round1.md",
            "Lineage verified.\n",
            sidecar={"required_prompt_contains": [f"item {ordinal} edit"]},
        )
    monkeypatch.chdir(root)
    source = load_workflow_source_for_preflight(root / "parent.md", root)
    asyncio.run(
        execute_workflow_run(
            config=config,
            source=source,
            force=False,
            no_live=True,
            console=Console(file=io.StringIO()),
            project_root=root,
        )
    )
    (run,) = (root / ".crewplane/execution-stages").iterdir()
    base = PreflightExecutionPlan.model_validate_json(
        (run / "preflight/execution-plan.json").read_bytes()
    )
    reconstructed = reconstruct_expansions(base, run)
    assert not reconstructed.invalid_groups
    effective = reconstructed.plan
    policies = {node.id: node.workspace_policy for node in effective.nodes}
    for ordinal in range(2):
        assert policies[f"chunks.i{ordinal}.analyze"].source_kind == "project"
        assert (
            policies[f"chunks.i{ordinal}.after"].source_node_id
            == f"chunks.i{ordinal}.analyze"
        )
        assert (
            policies[f"chunks.i{ordinal}.after"].logical_worktree_name
            == f"chunks.i{ordinal}.change"
        )
        captured = (
            root
            / ".crewplane/execution-results"
            / run.name
            / f"generated-files/chunks.i{ordinal}.analyze/alpha_executor_0/src/app.txt"
        )
        assert captured.read_bytes() == f"item {ordinal} edit\n".encode()
    assert (root / "src/app.txt").read_bytes() == b"base application\n"
    branches = isolated_git.run_text(
        root, "for-each-ref", "--format=%(refname)", "refs/heads/crewplane"
    ).splitlines()
    assert len(branches) == 2
    assert load_authoritative_plan(run, run.name).plan == effective
    summary = (run / "logs/summary.md").read_text()
    assert "chunks.i0.analyze" in summary and "chunks.i1.after" in summary
    template = base.foreach_templates[0].model_copy(deep=True)
    for node in template.nodes:
        if node.workspace_policy is not None:
            node.workspace_policy.branch_export.create_branch = False
    requested = base.model_copy(update={"foreach_templates": [template]})
    reconciled = reconstruct_expansions(requested, run)
    assert not reconciled.invalid_groups
    assert (
        reconciled.plan.expansions["chunks"].digest
        == effective.expansions["chunks"].digest
    )
    assert all(
        node.workspace_policy is None
        or not node.workspace_policy.branch_export.create_branch
        for node in reconciled.plan.nodes
    )
    assert template.workspace_file_locators
    for locator in template.workspace_file_locators:
        assert locator.source_span is not None
        locator.source_span = {
            **locator.source_span,
            "start_line": locator.source_span["start_line"] + 2,
            "end_line": locator.source_span["end_line"] + 2,
        }
    assert template.semantic_digest() == template.template_digest
    shifted = reconstruct_expansions(requested, run)
    assert not shifted.invalid_groups
    assert shifted.plan.expansions == reconciled.plan.expansions
    assert (
        shifted.plan.workspace_file_locators != reconciled.plan.workspace_file_locators
    )
    (run / "preflight/expansions/chunks/items/0.json").unlink()
    assert load_authoritative_plan(run, run.name).invalid


def test_literal_instances_reject_duplicate_explicit_branch_targets(
    tmp_path: Path, isolated_git: IsolatedGit, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace_project(tmp_path, isolated_git)
    compile_literal_foreach(root)
    child = root / "child.md"
    payload, body = workflow_document(child)
    payload["nodes"] = payload["nodes"][:2]
    payload["worktrees"] = {
        "change": {
            "kind": "worktree",
            "create_branch": True,
            "branch_name": "same-branch",
        }
    }
    write_workflow_document(child, payload, body.split("## independent")[0])
    config = workspace_config(tmp_path / "cache", tmp_path / "fixtures")
    config.settings.max_concurrent_nodes = 1
    config_path = root / "config.json"
    config_path.write_text(
        json.dumps(config.model_dump(mode="json")), encoding="utf-8", newline="\n"
    )
    (root / ".gitignore").write_bytes(b".crewplane/\n")
    isolated_git.run_text(root, "add", ".")
    isolated_git.run_text(root, "commit", "-m", "foreach workflow")
    monkeypatch.chdir(root)
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--tasks",
            str(root / "parent.md"),
            "--config",
            str(config_path),
            "--dry-run",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "branch target collision" in result.output


def test_shared_input_cannot_transfer_a_different_managed_caller_worktree(
    tmp_path: Path, isolated_git: IsolatedGit, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace_project(tmp_path, isolated_git)
    compile_literal_foreach(root, "[]")
    child = root / "child.md"
    payload, body = workflow_document(child)
    payload["worktrees"] = {"child": {"kind": "worktree"}}
    payload["inputs"]["request"] = "request"
    payload["nodes"].insert(
        1, {"id": "request", "mode": "input", "source": "{{file:ignored.md}}"}
    )
    payload["nodes"][2]["needs"].append("request")
    write_workflow_document(child, payload, body + "\nRead {{request.output}}.")
    parent = root / "parent.md"
    payload, body = workflow_document(parent)
    payload["worktrees"] = {"caller": {"kind": "worktree"}}
    payload["nodes"][0].update(needs=["request"], inputs={"request": "request"})
    payload["nodes"].insert(
        0, {"id": "request", "mode": "parallel", "providers": ["alpha"]}
    )
    write_workflow_document(
        parent, payload, "## request\nPrepare shared data.\n" + body
    )
    config = workspace_config(tmp_path / "cache", tmp_path / "fixtures")
    config.settings.max_concurrent_nodes = 1
    monkeypatch.chdir(root)
    preview = compile_foreach_file(parent, config)
    assert preview.has_errors()
    assert "Automatic merge between logical worktrees is not supported" in str(
        preview.diagnostics
    )
    assert not (root / ".crewplane/execution-stages").exists()

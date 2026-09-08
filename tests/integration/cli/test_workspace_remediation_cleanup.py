from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from crewplane.cli.app import app
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.version import SCHEMA_VERSION
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.isolated_git import IsolatedGit
from tests.helpers.workspace_workflow_fixtures import (
    review_output,
    run_dirs,
    workspace_states,
)

isolated_git = isolated_git_support.isolated_git


@pytest.mark.parametrize("failure", ["context_exhausted", "ordinary", "tampered"])
def test_remediation_failure_cleanup_preserves_artifact_drift_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: IsolatedGit,
    failure: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workflow = project / "workflow.task.md"
    workflow.write_text(_workflow_source(), encoding="utf-8")
    isolated_git.run_text(project, "init")
    isolated_git.run_text(project, "add", "workflow.task.md")
    isolated_git.run_text(project, "commit", "-m", "initial")
    counter = tmp_path / "calls.txt"
    counter.write_text("0", encoding="utf-8")
    provider = tmp_path / "provider.py"
    provider.write_text(_provider_source(project, counter, failure), encoding="utf-8")
    config = Config(
        version=SCHEMA_VERSION,
        agents={
            "alpha": AgentConfig(
                cli_cmd=[sys.executable, provider.as_posix()],
                provider_kind="generic",
                max_retries=0,
            )
        },
        settings=Settings(
            workspace={
                "enabled": True,
                "clean_start": "tracked_only",
                "cache_root": (tmp_path / "cache").as_posix(),
            },
            integrations={"ui": {"implementation": "none"}},
        ),
    )
    state_dir = project / ".crewplane"
    state_dir.mkdir()
    (state_dir / "config.yml").write_text(config.model_dump_json(), encoding="utf-8")
    monkeypatch.chdir(project)

    result = CliRunner().invoke(
        app,
        ["run", "--tasks", workflow.as_posix(), "--no-live"],
    )

    assert result.exit_code == (0 if failure == "context_exhausted" else 1), (
        result.output
    )
    assert ("modified fatal artifacts" in result.output) is (failure == "tampered")
    assert counter.read_text(encoding="utf-8") == "3"
    run_dir = run_dirs(project)[0]
    states = workspace_states(run_dir / "implement")
    assert {
        (state["role"], state["round_num"], state["status"]) for state in states
    } == {
        ("executor", 1, "succeeded"),
        ("reviewer", 1, "succeeded"),
        ("executor", 2, "failed"),
    }
    assert all(state["workflow_name"] == "RemediationCleanup" for state in states)
    assert all(state["workspace"]["retention"] == "deleted" for state in states)
    workspace_paths = {Path(state["execution"]["workspace_path"]) for state in states}
    assert all(not path.exists() for path in workspace_paths)
    assert len(workspace_paths) == 2
    manifest = json.loads(
        (run_dir / "manifests" / "run.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == (
        "succeeded" if failure == "context_exhausted" else "failed"
    )


def _workflow_source() -> str:
    return f"""---
schema_version: '{SCHEMA_VERSION}'
name: RemediationCleanup
worktrees:
  code:
    kind: worktree
nodes:
  - id: implement
    mode: sequential
    depth: 1
    continue_on_failure: true
    providers:
      - provider: alpha
        role: executor
      - provider: alpha
        role: reviewer
---
## implement
Implement and review the application.
"""


def _provider_source(project: Path, counter: Path, failure: str) -> str:
    message = (
        "Provider failed."
        if failure == "ordinary"
        else "The context window is full. Start a new session."
    )
    review = review_output("CHANGES_REQUESTED", "- Fix the remaining bug")
    return f"""import json
import sys
from pathlib import Path

sys.stdin.read()
counter = Path({counter.as_posix()!r})
count = int(counter.read_text()) + 1
counter.write_text(str(count))
if count == 1:
    print("Implemented candidate.")
elif count == 2:
    print({review!r})
else:
    if {failure == "tampered"!r}:
        state = next(Path({project.as_posix()!r}).glob(
            ".crewplane/execution-stages/*/implement/"
            "workspace-state-implement-alpha_executor_0-round1*.json"
        ))
        payload = json.loads(state.read_text())
        payload["workflow_name"] = "Provider tampered"
        state.write_text(json.dumps(payload))
    print({message!r}, file=sys.stderr)
    sys.exit(1)
"""

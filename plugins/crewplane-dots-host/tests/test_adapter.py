import asyncio
import io
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from crewplane_dots_host import Queue
from rich.console import Console

from crewplane.architecture.loader import instantiate_adapter
from crewplane.bootstrap.runtime_config import build_runtime_config_snapshot
from crewplane.core.config import Config, load_config
from crewplane.runtime.agent.invocation import command as invocation_command
from tests import conftest
from tests.helpers import isolated_git as git_helpers
from tests.helpers.isolated_git import IsolatedGit
from tests.helpers.platforms import requires_workspace_support

isolated_git = git_helpers.isolated_git
isolated_test_environment = conftest.isolated_test_environment


@pytest.fixture
def config(tmp_path: Path) -> Config:
    example = (
        Path(__file__).parents[1] / "examples" / "review-and-summary" / "config.yml"
    )
    config = load_config(example)
    config.agents["dots"].cli_cmd = [
        str(Path(sys.executable).parent / "crewplane-dots-host"),
        "--queue",
        str(tmp_path / "queue"),
        "invoke",
    ]
    config.agents["dots"].extra_args = ["--reasoning", "xhigh", "--timeout", "5"]
    return config


@requires_workspace_support
@pytest.mark.parametrize("kind", ["snapshot", "worktree"])
def test_managed_run_rejected_before_queue_submission(
    tmp_path: Path, isolated_git: IsolatedGit, config: Config, kind: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".gitignore").write_text(".crewplane/\n", encoding="utf-8")
    isolated_git.run(project, "init")
    isolated_git.run(project, "add", ".gitignore")
    isolated_git.run(project, "commit", "-m", "initial")
    config.settings.workspace.enabled = True
    config.settings.workspace.cache_root = str(tmp_path / "cache")
    config.agents["dots"].extra_args = ["--timeout", "0.01"]
    state_dir = project / ".crewplane"
    state_dir.mkdir()
    config_file = state_dir / "config.yml"
    config_file.write_text(
        yaml.safe_dump(config.model_dump(mode="json", exclude_unset=True)),
        encoding="utf-8",
    )
    workflow_file = state_dir / "review.task.md"
    workflow_file.write_text(
        f"""---
schema_version: "1.0"
name: Dots workspace guard
worktrees:
  scratch:
    kind: {kind}
nodes:
  - id: review
    mode: parallel
    providers: [dots]
---

## review

This request must never reach the host.
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from crewplane.cli.app import app; app()",
            "run",
            "-c",
            str(config_file),
            "-t",
            str(workflow_file),
            "--no-live",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "WORKSPACE-INVOKER" in result.stdout + result.stderr
    assert not (tmp_path / "queue").exists()
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("idle_timeout", [None, 0.2])
def test_workspace_disabled_invocation_publishes_output(
    tmp_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    idle_timeout: float | None,
) -> None:
    config.agents["dots"].invocation_idle_timeout_seconds = idle_timeout
    runner = AsyncMock(wraps=invocation_command.run_command_once)
    monkeypatch.setattr(invocation_command, "run_command_once", runner)
    snapshot = build_runtime_config_snapshot(config, Console(file=io.StringIO()), True)
    adapter = instantiate_adapter(
        "invoker", config.settings.integrations.invoker.implementation
    )
    invoker = adapter.create_invoker(config, snapshot.invoker_options)
    agent = config.agents["dots"]
    output = tmp_path / "output.md"

    async def run() -> None:
        with closing(Queue(tmp_path / "queue")) as queue:
            # The test host can wait; invocation connections need short signal latency.
            queue.db.execute("PRAGMA busy_timeout=5000")
            task = asyncio.create_task(
                invoker.invoke(agent, agent.default_model, "Review", output, tmp_path)
            )
            try:
                async with asyncio.timeout(10):
                    while not (jobs := queue.inspect()):
                        if task.done():
                            await task
                        await asyncio.sleep(0.01)
                    job = queue.claim(jobs[0]["id"], 10)
                    assert job["request"]["working_directory"] == str(tmp_path)
                    queue.complete(
                        job["id"],
                        job["token"],
                        {
                            "output": "Local host fixture result",
                            "host_agent_id": "test-host",
                            "confirmed": {
                                "model": agent.default_model,
                                "reasoning": "xhigh",
                            },
                            "evidence": "Deterministic protocol fixture",
                        },
                    )
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert output.read_text(encoding="utf-8").strip() == "Local host fixture result"
    assert runner.await_args.kwargs["idle_timeout_seconds"] is None

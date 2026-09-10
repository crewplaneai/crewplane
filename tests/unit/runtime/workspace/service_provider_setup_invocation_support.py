from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from crewplane.architecture.contracts import InvocationContext
from crewplane.core.config import AgentConfig
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSetupCommandRecord,
    WorkspaceSetupRecord,
)


def start_git_metadata_lock_holder(
    lock_path: Path,
    hold_seconds: float = 1,
) -> subprocess.Popen[str]:
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, pathlib, sys, time; "
                "handle = pathlib.Path(sys.argv[1]).open('a+b'); "
                "fcntl.flock(handle.fileno(), fcntl.LOCK_EX); "
                "print('locked', flush=True); "
                "time.sleep(float(sys.argv[2]))"
            ),
            lock_path.as_posix(),
            str(hold_seconds),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "locked"
    return holder


def stop_git_metadata_lock_holder(holder: subprocess.Popen[str]) -> None:
    holder.terminate()
    holder.wait(timeout=2)
    if holder.stdout is not None:
        holder.stdout.close()


class SetupMarkerInvoker:
    def __init__(self, expect_marker: bool) -> None:
        self.expect_marker = expect_marker
        self.calls = 0

    async def invoke(
        self,
        config: AgentConfig,
        model: str | None,
        prompt: str,
        output_file: Path,
        cwd: Path,
        log_file: Path | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> None:
        del config, model, prompt, log_file, invocation_context
        self.calls += 1
        marker = cwd / "setup-marker.txt"
        assert marker.exists() is self.expect_marker
        if self.expect_marker:
            assert marker.read_text(encoding="utf-8") == "ready"
        output_file.write_text("provider completed\n", encoding="utf-8")

    def log_presentation_for(self, config: AgentConfig) -> None:
        del config
        return None


def plan_with_setup(
    plan: PreflightExecutionPlan,
    commands: list[list[str]],
) -> PreflightExecutionPlan:
    plan = plan_with_available_setup_profile(plan, commands)
    node = plan.nodes[0]
    policy = node.workspace_policy
    assert policy is not None
    updated_policy = policy.model_copy(
        update={
            "setup": WorkspaceSetupRecord(
                profile_name="bootstrap",
                commands=[
                    WorkspaceSetupCommandRecord(argv=argv, command_index=index)
                    for index, argv in enumerate(commands)
                ],
            )
        }
    )
    return plan.model_copy(
        update={
            "nodes": [node.model_copy(update={"workspace_policy": updated_policy})],
        }
    )


def plan_with_available_setup_profile(
    plan: PreflightExecutionPlan,
    commands: list[list[str]],
) -> PreflightExecutionPlan:
    runtime_snapshot = dict(plan.runtime_config_snapshot)
    workspace = dict(runtime_snapshot.get("workspace", {}))
    workspace["setup_profiles"] = {"bootstrap": {"run": commands}}
    runtime_snapshot["workspace"] = workspace
    return plan.model_copy(update={"runtime_config_snapshot": runtime_snapshot})

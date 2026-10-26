import asyncio
import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import typer

from crewplane.cli.run.execution import execute_workflow_run
from crewplane.core.review_contract import render_no_findings_review_contract
from crewplane.core.workflow.models import ProviderSpec
from tests.helpers.cli_provider import provider_source, quiet_console
from tests.helpers.ollama_agent import (
    ScriptedOllama,
    command_call,
    final_response,
    ollama_server,
    tool_response,
)
from tests.helpers.ollama_agent_workflow import configured_agent
from tests.helpers.platforms import requires_resume_support

pytestmark = pytest.mark.usefixtures("ollama_package")


@pytest.mark.parametrize("valid", [True, False])
def test_real_agent_preserves_read_only_reviewer_prompt_and_contract(tmp_path, valid):
    def review(payload):
        assert len(payload["messages"]) == 2
        prompt = payload["messages"][1]["content"]
        assert "You are acting only as a reviewer." in prompt
        assert (
            "Do not modify files, apply fixes, run changes, or change the workspace."
            in prompt
        )
        assert "candidate result" in prompt
        assert "read-only" in payload["messages"][0]["content"]
        return final_response(
            render_no_findings_review_contract() if valid else "invalid review contract"
        )

    script = ScriptedOllama(
        [final_response("candidate result"), review]
        + ([] if valid else [final_response("candidate result")])
    )
    source = provider_source()
    source.workflow.nodes[0].depth = 1
    source.workflow.nodes[0].audit_rounds = 1
    source.workflow.nodes[0].providers.append(
        ProviderSpec(provider="local", role="reviewer")
    )
    source = replace(source, root_workflow_path=tmp_path / "workflow.task.md")
    with ollama_server(script) as endpoint:
        _, config, _ = configured_agent(tmp_path, endpoint)
        config.settings.sequential_consensus_on_exhaustion = "fatal"
        pending = execute_workflow_run(
            config, source, False, True, quiet_console(), project_root=tmp_path
        )
        if valid:
            asyncio.run(pending)
        else:
            with pytest.raises((RuntimeError, typer.Exit)):
                asyncio.run(pending)
    manifests = list(
        (tmp_path / ".crewplane/execution-stages").glob("*/manifests/run.json")
    )
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text())["status"] == (
        "succeeded" if valid else "failed"
    )
    assert not script.replies


@requires_resume_support
def test_interrupted_loop_is_not_resumed_but_completed_node_is_reused(tmp_path):
    def fail_after_effect(payload):
        assert json.loads(payload["messages"][-1]["content"])["exit_code"] == 0
        return 503, b"interrupted model round"

    def restarted(payload):
        assert len(payload["messages"]) == 2
        assert "completed upstream" in payload["messages"][1]["content"]
        assert (tmp_path / "partial-effect").read_text() == "not rolled back"
        return final_response("downstream restarted from workflow checkpoint")

    script = ScriptedOllama(
        [
            final_response("completed upstream"),
            tool_response(
                command_call(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('partial-effect').write_text('not rolled back')",
                    ]
                )
            ),
            fail_after_effect,
            restarted,
            final_response("forced upstream"),
            final_response("forced downstream"),
        ]
    )
    source = replace(
        provider_source(downstream=True),
        root_workflow_path=tmp_path / "workflow.task.md",
    )
    with (
        ollama_server(script) as endpoint,
        patch("crewplane.artifacts.directory_manager.datetime") as clock,
    ):
        clock.now.side_effect = [
            datetime(2026, 10, 25) + timedelta(seconds=index) for index in range(20)
        ]
        _, config, _ = configured_agent(tmp_path, endpoint)

        def run(force=False):
            return asyncio.run(
                execute_workflow_run(
                    config, source, force, True, quiet_console(), project_root=tmp_path
                )
            )

        with pytest.raises((RuntimeError, typer.Exit)):
            run()
        assert not list(
            (tmp_path / ".crewplane/execution-results").glob("*/downstream-result.md")
        )
        run()
        executions = [item for item in script.requests if "tools" in item]
        assert len(executions) == 4
        stages = tmp_path / ".crewplane/execution-stages"
        manifests = [
            json.loads(path.read_text())
            for path in sorted(stages.glob("*/manifests/run.json"))
        ]
        assert manifests[-1]["resumed_nodes"] == ["upstream"]
        run()
        assert len([item for item in script.requests if "tools" in item]) == 4
        run(True)
        assert len([item for item in script.requests if "tools" in item]) == 6
    assert not script.replies

import asyncio
import json
import sys

import pytest

from crewplane.cli.run.execution import execute_workflow_run
from crewplane.core.config import AgentConfig
from crewplane.core.preflight import PreflightWorkflowSource
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.core.workspace.settings import WorkspaceSetupProfile
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.cli_provider import OLLAMA_PACKAGE, provider_config, quiet_console
from tests.helpers.ollama_agent import (
    ScriptedOllama,
    agent_executable,
    command_call,
    final_response,
    ollama_server,
    tool_response,
)
from tests.helpers.platforms import requires_workspace_support
from tests.helpers.workspace_workflow_runner import workspace_project

pytestmark = [requires_workspace_support, pytest.mark.usefixtures("ollama_package")]
isolated_git = isolated_git_support.isolated_git


def workspace_agent_config(executable, endpoint, cache):
    config = provider_config(
        AgentConfig(
            cli_cmd=[str(executable)],
            provider_kind="ollama",
            default_model="test-model",
            invocation_timeout_seconds=30,
            environment={
                "OLLAMA_HOST": endpoint,
                "PYTHONPATH": str(OLLAMA_PACKAGE / "src"),
            },
        ),
        {
            "providers": {
                "ollama": {
                    "implementation": "crewplane_ollama:OLLAMA",
                    "distribution": "crewplane-ollama",
                }
            }
        },
    )
    config.settings.workspace.enabled = True
    config.settings.workspace.cache_root = str(cache)
    config.settings.workspace.cleanup_on_success = False
    return config


def workflow_for(project, kind):
    nodes = [
        WorkflowNode(
            id="implement",
            mode="sequential",
            providers=[ProviderSpec(provider="local")],
            prompt_segments=[
                PromptSegment(
                    content="Use skills/feature/SKILL.md and its resource; implement the feature and validate it.",
                    role="shared",
                )
            ],
        ),
        WorkflowNode(
            id="consume",
            needs=["implement"],
            mode="sequential",
            providers=[ProviderSpec(provider="local")],
            prompt_segments=[
                PromptSegment(
                    content="Read current source without editing. {{implement.output}}",
                    role="shared",
                )
            ],
        ),
    ]
    workflow = WorkflowPlan(
        name="agent-workspaces", worktrees={"selected": {"kind": kind}}, nodes=nodes
    )
    return PreflightWorkflowSource.from_workflow(
        workflow, root_workflow_path=project / "workflow.task.md"
    )


@pytest.mark.parametrize("kind", ["worktree", "snapshot"])
def test_real_agent_skill_resources_workspace_capture_and_lineage(
    tmp_path, monkeypatch, isolated_git, kind
):
    project = workspace_project(tmp_path, isolated_git)
    skill = project / "skills/feature"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "Use resource.txt and validate.py beside this file.\n"
    )
    (skill / "resource.txt").write_text("new application\n")
    (skill / "validate.py").write_text(
        "from pathlib import Path\nassert Path('src/app.txt').read_text() == 'new application\\n'\nprint('PASS')\n"
    )
    isolated_git.run_text(project, "add", "skills")
    isolated_git.run_text(project, "commit", "-m", "tracked explicit skill")
    monkeypatch.chdir(project)
    observed = []
    read = "from pathlib import Path; import json, os; print(json.dumps({'cwd': str(Path.cwd()), 'git_config': os.environ.get('GIT_CONFIG_NOSYSTEM'), 'skill': Path('skills/feature/SKILL.md').read_text(), 'resource': Path('skills/feature/resource.txt').read_text(), 'app': Path('src/app.txt').read_text()}))"

    def inspect_then_edit(payload):
        feedback = json.loads(payload["messages"][-1]["content"])
        observed.append(json.loads(feedback["stdout"]))
        assert observed[-1]["skill"].startswith("Use resource.txt")
        assert observed[-1]["resource"] == "new application\n"
        assert observed[-1]["app"] == "base application\n"
        assert observed[-1]["git_config"] == "1"
        return tool_response(
            command_call(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('src/app.txt').write_text(Path('skills/feature/resource.txt').read_text())",
                ]
            ),
            command_call([sys.executable, "skills/feature/validate.py"]),
        )

    def verified(payload):
        result = json.loads(payload["messages"][-1]["content"])
        assert result["exit_code"] == 0 and result["stdout"] == "PASS\n"
        return final_response("Implemented src/app.txt and passed skill validation.")

    def consume(payload):
        result = json.loads(payload["messages"][-1]["content"])
        observed.append(json.loads(result["stdout"]))
        assert observed[-1]["app"] == (
            "new application\n" if kind == "worktree" else "base application\n"
        )
        return final_response("Consumed selected workspace source.")

    script = ScriptedOllama(
        [
            tool_response(command_call([sys.executable, "-c", read])),
            inspect_then_edit,
            verified,
            tool_response(command_call([sys.executable, "-c", read])),
            consume,
        ]
    )
    with ollama_server(script) as endpoint:
        config = workspace_agent_config(
            agent_executable(tmp_path), endpoint, tmp_path / "cache"
        )
        asyncio.run(
            execute_workflow_run(
                config,
                workflow_for(project, kind),
                False,
                True,
                quiet_console(),
                project_root=project,
            )
        )
    assert not script.replies
    assert all(item["cwd"] != str(project) for item in observed)
    assert (project / "src/app.txt").read_text() == "base application\n"
    stage = next((project / ".crewplane/execution-stages").iterdir())
    states = [
        json.loads(path.read_text()) for path in stage.rglob("workspace-state*.json")
    ]
    implemented = next(state for state in states if state["node_id"] == "implement")
    if kind == "worktree":
        commit = implemented["result"]["result_commit"]
        assert (
            isolated_git.run_text(project, "show", f"{commit}:src/app.txt")
            == "new application"
        )
        assert any(
            state["source"]["node_id"] == "implement"
            for state in states
            if state["node_id"] == "consume"
        )
    else:
        assert all(
            state.get("result", {}).get("result_commit") is None for state in states
        )
        assert all(state["source"]["node_id"] is None for state in states)
    captured = list(
        (project / ".crewplane/execution-results").glob(
            "*/generated-files/implement/**/src/app.txt"
        )
    )
    assert len(captured) == 1 and captured[0].read_text() == "new application\n"


@pytest.mark.parametrize("kind", ["worktree", "snapshot"])
def test_provider_retry_starts_fresh_conversation_and_restores_workspace(
    tmp_path, monkeypatch, isolated_git, kind
):
    project = workspace_project(tmp_path, isolated_git)
    monkeypatch.chdir(project)
    source = workflow_for(project, kind)
    source.workflow.nodes = source.workflow.nodes[:1]
    marker = tmp_path / "external-attempts"

    def fail_after_edit(payload):
        assert json.loads(payload["messages"][-1]["content"])["exit_code"] == 0
        return 503, b"transient server failure"

    def fresh(payload):
        assert len(payload["messages"]) == 2
        return tool_response(
            command_call(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; print(Path('src/app.txt').read_text()); print(Path('setup.txt').read_text())",
                ]
                if kind == "worktree"
                else [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; print(Path('src/app.txt').read_text())",
                ]
            )
        )

    def finish(payload):
        result = json.loads(payload["messages"][-1]["content"])
        assert result["stdout"].startswith("base application\n")
        if kind == "worktree":
            assert "setup ready" in result["stdout"]
        return final_response("Fresh retry verified.")

    mutate = (
        "from pathlib import Path; Path('src/app.txt').write_text('dirty first attempt'); "
        + f"Path({str(marker)!r}).write_text('external side effect persists')"
    )
    script = ScriptedOllama(
        [
            tool_response(command_call([sys.executable, "-c", mutate])),
            fail_after_edit,
            fresh,
            finish,
        ]
    )
    with ollama_server(script) as endpoint:
        config = workspace_agent_config(
            agent_executable(tmp_path), endpoint, tmp_path / "cache"
        )
        agent = config.agents["local"]
        agent.max_retries = 1
        agent.retry_delay_seconds = 0
        agent.retry_on_exit_codes = [1]
        if kind == "worktree":
            config.settings.workspace.setup_profiles = {
                "prepare": WorkspaceSetupProfile(
                    run=[
                        [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; Path('setup.txt').write_text('setup ready')",
                        ]
                    ]
                )
            }
            source.workflow.worktrees["selected"].setup_profile = "prepare"
        asyncio.run(
            execute_workflow_run(
                config, source, False, True, quiet_console(), project_root=project
            )
        )
    assert marker.read_text() == "external side effect persists"
    assert not script.replies

import asyncio
import json
import sys
from dataclasses import replace

import pytest

from crewplane.cli.run.execution import execute_workflow_run
from crewplane.core.config import AgentConfig
from crewplane.core.workflow.models import PromptSegment
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.cli_provider import (
    OLLAMA_PACKAGE,
    provider_config,
    provider_source,
    quiet_console,
)
from tests.helpers.ollama_agent import (
    ScriptedOllama,
    agent_executable,
    command_call,
    final_response,
    ollama_server,
    tool_response,
)

pytestmark = pytest.mark.usefixtures("ollama_package")
isolated_git = isolated_git_support.isolated_git


def write_skill(root):
    skill = root / "skills" / "selected feature"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "Read requirement.txt beside this file; edit feature.py; run validate.py from this skill directory without changing cwd.\n"
    )
    (skill / "requirement.txt").write_text("Required return value: café\n")
    (skill / "validate.py").write_text(
        "from pathlib import Path\nimport runpy\nassert runpy.run_path('feature.py')['value'] == 'café'\nprint('validated ' + str(Path.cwd()))\n",
        encoding="utf-8",
    )
    (root / "feature.py").write_text("value = 'old'\n")
    return skill


def feature_replies(skill, root):
    def after_read(payload):
        result = json.loads(payload["messages"][-1]["content"])
        assert "Required return value: café" in result["stdout"]
        assert "Read requirement.txt" in result["stdout"]
        context = json.loads(result["stdout"].splitlines()[-1])
        assert context["cwd"] == str(root)
        assert context["environment"] == "explicit marker"
        return tool_response(
            command_call(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('feature.py').write_text(\"value = 'wrong'\\n\")",
                ]
            )
        )

    def failed_test(payload):
        result = json.loads(payload["messages"][-1]["content"])
        assert result["exit_code"] == 1
        assert "AssertionError" in result["stderr"]
        return tool_response(
            command_call(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('feature.py').write_text(\"value = 'café'\\n\", encoding='utf-8')",
                ]
            )
        )

    def passed_test(payload):
        result = json.loads(payload["messages"][-1]["content"])
        assert result["exit_code"] == 0
        assert result["stdout"] == f"validated {root}\n"
        return final_response(
            "Updated `feature.py`; validation passed after correcting the failed test.\n\n## Generated Files\n- `feature.py`"
        )

    read = (
        "from pathlib import Path; import json, os; "
        + f"p = Path({str(skill)!r}); "
        + "print((p/'SKILL.md').read_text()); print((p/'requirement.txt').read_text()); print(json.dumps({'cwd': str(Path.cwd()), 'environment': os.environ['SKILL_MARKER']}))"
    )
    return [
        tool_response(command_call([sys.executable, "-c", read])),
        after_read,
        tool_response(command_call([sys.executable, str(skill / "validate.py")])),
        failed_test,
        tool_response(command_call([sys.executable, str(skill / "validate.py")])),
        passed_test,
    ]


def configuration(executable, endpoint, log_capture=True):
    options = {
        "providers": {
            "ollama": {
                "implementation": "crewplane_ollama:OLLAMA",
                "distribution": "crewplane-ollama",
            }
        }
    }
    config = provider_config(
        AgentConfig(
            cli_cmd=[str(executable)],
            provider_kind="ollama",
            default_model="test-model",
            environment={
                "OLLAMA_HOST": endpoint,
                "PYTHONPATH": str(OLLAMA_PACKAGE / "src"),
                "SKILL_MARKER": "explicit marker",
            },
            invocation_timeout_seconds=30,
        ),
        options,
    )
    config.settings.integrations.artifacts.options = {"log_cli_output": log_capture}
    return config


@pytest.mark.parametrize("included", [False, True])
@pytest.mark.parametrize("log_capture", [False, True])
def test_real_workflow_skill_read_edit_failure_correction_and_capture(
    tmp_path, monkeypatch, isolated_git, included, log_capture
):
    project = tmp_path / "project with spaces"
    project.mkdir()
    skill = write_skill(project)
    isolated_git.run_text(project, "init")
    isolated_git.run_text(project, "add", "feature.py", "skills")
    isolated_git.run_text(project, "commit", "-m", "initial feature and explicit skill")
    executable = agent_executable(tmp_path)
    script = ScriptedOllama(feature_replies(skill, project))
    source = provider_source()
    prompt = (
        "Use the included skill; source directory: skills/selected feature.\n{{file:skills/selected feature/SKILL.md}}"
        if included
        else "Use skills/selected feature/SKILL.md; follow its linked resource and script."
    )
    source.workflow.nodes[0].prompt_segments = [
        PromptSegment(content=prompt, role="shared")
    ]
    source = replace(source, root_workflow_path=project / "workflow.task.md")
    monkeypatch.chdir(project)
    with ollama_server(script) as endpoint:
        config = configuration(executable, endpoint, log_capture)
        asyncio.run(
            execute_workflow_run(
                config, source, False, True, quiet_console(), project_root=project
            )
        )
    assert (project / "feature.py").read_text() == "value = 'café'\n"
    assert not script.replies
    execution_requests = [request for request in script.requests if "tools" in request]
    rendered = execution_requests[0]["messages"][1]["content"]
    assert "source directory" in rendered if included else "SKILL.md" in rendered
    assert "{{file:" not in rendered
    results = project / ".crewplane/execution-results"
    generated = list(results.glob("*/generated-files/upstream/**/feature.py"))
    assert len(generated) == 1 and generated[0].read_text() == "value = 'café'\n"
    output = next(results.glob("*/upstream-result.md")).read_text()
    assert "validation passed" in output and "AssertionError" not in output
    logs = list((project / ".crewplane/execution-stages").rglob("*.log"))
    combined = "\n".join(path.read_text(errors="replace") for path in logs)
    assert ("tool 1 start" in combined) is log_capture


def test_missing_skill_resource_and_failed_script_stay_visible(tmp_path):
    from tests.helpers.ollama_agent import invoke_agent

    def failed(payload):
        result = json.loads(payload["messages"][-1]["content"])
        assert result["exit_code"] != 0
        assert "FileNotFoundError" in result["stderr"]
        return final_response(
            "Blocked: required skill resource missing.txt is unavailable; affected work stopped."
        )

    script = ScriptedOllama(
        [
            tool_response(
                command_call(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('missing.txt').read_text()",
                    ]
                )
            ),
            failed,
        ]
    )
    with ollama_server(script) as endpoint:
        result = invoke_agent(
            tmp_path,
            endpoint,
            "Follow explicitly selected skill; do not omit its requirements.",
        )
    assert result.returncode == 0
    assert b"Blocked:" in result.stdout
    assert not (tmp_path / "feature.py").exists()

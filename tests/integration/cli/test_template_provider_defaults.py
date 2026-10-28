import asyncio
import io
from pathlib import Path

import pytest
from rich.console import Console

from crewplane.adapters.invokers.cli_invoker import build_cli_invocation_plan
from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
    InvocationContext,
)
from crewplane.cli.onboarding import rendering_defaults
from crewplane.cli.onboarding.rendering import (
    manual_config_snippet,
    render_provider_ready_config,
    rendered_default_config,
)
from crewplane.cli.onboarding.rendering_config_agents import (
    extract_commented_provider_block,
)
from crewplane.cli.workflow_runner import compile_workflow_preview
from crewplane.core.config import load_config
from crewplane.core.preflight import load_workflow_source_for_preflight
from crewplane.core.yaml_loader import load_yaml_unique
from crewplane.runtime.agent.invoker import invoke_agent_with_runner
from crewplane.version import SCHEMA_VERSION

pytestmark = pytest.mark.usefixtures("posix_cli_plans")

PROMPT = "Return a short review."


def _write_codex_template(tmp_path: Path, model_field: str | None) -> Path:
    template = rendering_defaults.CONFIG_TEMPLATE.read_text(encoding="utf-8")
    block = extract_commented_provider_block(template, "codex")
    lines = [
        line
        for line in block.splitlines(keepends=True)
        if not line.startswith("  #   default_model:")
    ]
    if model_field is not None:
        lines.insert(1, f"  #   default_model: {model_field}\n")
    template_path = tmp_path / "template.yml"
    template_path.write_text(
        template.replace(block, "".join(lines), 1),
        encoding="utf-8",
        newline="\n",
    )
    return template_path


@pytest.fixture(params=["shipped", "omitted"], ids=["shipped-source", "omitted-source"])
def codex_source_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    if request.param == "shipped":
        return
    template = rendering_defaults.CONFIG_TEMPLATE.read_text(encoding="utf-8")
    block = extract_commented_provider_block(template, "codex")
    source_path = tmp_path / "source-template.yml"
    source_path.write_text(
        template.replace(
            block,
            "  # codex:\n"
            "  #   cli_cmd: [codex, exec]\n"
            "  #   provider_kind: codex\n"
            "  #   prompt_transport: stdin\n"
            '  #   prompt_transport_arg: "-"\n',
            1,
        ),
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr(rendering_defaults, "CONFIG_TEMPLATE", source_path)


class CapturingCodexRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    async def __call__(
        self,
        cmd: list[str],
        stdin_data: bytes | None,
        log_file: Path | None,
        append_log: bool,
        log_header: bytes | None,
        cwd: Path,
        invocation_context: InvocationContext | None,
        idle_timeout_seconds: float | None,
        child_environment: ChildProcessEnvironment | None = None,
    ) -> CommandResult:
        del (
            stdin_data,
            log_file,
            append_log,
            log_header,
            cwd,
            invocation_context,
            idle_timeout_seconds,
            child_environment,
        )
        self.commands.append(cmd.copy())
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text(
            "Template propagation succeeded.\n", encoding="utf-8", newline="\n"
        )
        return CommandResult(returncode=0, stdout_text="", stderr_text="")


@pytest.mark.usefixtures("codex_source_template")
@pytest.mark.parametrize(
    ("model_field", "workflow_model", "expected_default", "expected_model"),
    [
        pytest.param(
            '"future-codex-model"',
            None,
            "future-codex-model",
            "future-codex-model",
            id="synthetic-default",
        ),
        pytest.param(
            '"future-codex-model"',
            "workflow-codex-model",
            "future-codex-model",
            "workflow-codex-model",
            id="workflow-override",
        ),
        pytest.param(None, None, None, None, id="omitted-default"),
        pytest.param("null", None, None, None, id="null-default"),
    ],
)
def test_template_model_reaches_config_preflight_and_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_field: str | None,
    workflow_model: str | None,
    expected_default: str | None,
    expected_model: str | None,
) -> None:
    template_path = _write_codex_template(tmp_path, model_field)
    monkeypatch.setattr(rendering_defaults, "CONFIG_TEMPLATE", template_path)
    default_config = rendered_default_config()
    rendered = render_provider_ready_config(default_config, ("codex",))
    snippet = manual_config_snippet(default_config, ("codex",))
    for agent in (
        load_yaml_unique(rendered)["agents"]["codex"],
        load_yaml_unique(snippet)["codex"],
    ):
        if model_field is None:
            assert "default_model" not in agent
        else:
            assert agent["default_model"] == expected_default

    state_dir = tmp_path / ".crewplane"
    state_dir.mkdir()
    config_path = state_dir / "config.yml"
    config_path.write_text(rendered, encoding="utf-8", newline="\n")
    config = load_config(config_path)
    assert config.agents["codex"].default_model == expected_default

    provider = (
        "codex"
        if workflow_model is None
        else f"{{provider: codex, model: {workflow_model}}}"
    )
    workflow_path = tmp_path / "review.task.md"
    workflow_path.write_text(
        f'---\nschema_version: "{SCHEMA_VERSION}"\n'
        "name: Template model propagation\nnodes:\n"
        f"  - id: review\n    mode: parallel\n    providers: [{provider}]\n"
        f"---\n## review\n{PROMPT}\n",
        encoding="utf-8",
        newline="\n",
    )
    source = load_workflow_source_for_preflight(workflow_path, project_root=tmp_path)
    preview = compile_workflow_preview(
        config=config,
        source=source,
        console=Console(file=io.StringIO(), force_terminal=False),
        no_live=True,
        fingerprint_key_policy="read_only",
        project_root=tmp_path,
        state_dir=state_dir,
        check_cli_availability=False,
    )
    assert not preview.has_errors(), preview.diagnostics
    assert preview.workflow_signature
    assert preview.runtime_config_snapshot is not None
    assert (
        preview.runtime_config_snapshot.agents["codex"].default_model
        == expected_default
    )
    [node] = preview.nodes
    [record] = node.provider_records
    assert record.provider == "codex"
    assert record.model == expected_model
    assert record.agent_config_signature
    assert record.invoker_config_signature

    runner = CapturingCodexRunner()
    output_file = tmp_path / "output.txt"
    asyncio.run(
        invoke_agent_with_runner(
            config=config.agents[record.agent_config_key],
            model=record.model,
            prompt=PROMPT,
            output_file=output_file,
            cwd=tmp_path,
            log_file=None,
            invocation_context=None,
            command_runner=runner,
            plan_builder=build_cli_invocation_plan,
        )
    )
    assert output_file.read_bytes() == b"Template propagation succeeded.\n"
    [command] = runner.commands
    assert command.count("--model") == (0 if expected_model is None else 1)
    assert "-m" not in command
    assert not any(argument.startswith(("--model=", "-m=")) for argument in command)
    if expected_model is not None:
        assert command[command.index("--model") + 1] == expected_model
    if workflow_model is not None:
        assert "future-codex-model" not in command

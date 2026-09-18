import asyncio
import json
import sys

import pytest

from crewplane.adapters.invokers.cli import CliInvokerAdapter
from crewplane.architecture.contracts import InvocationContext
from crewplane.cli.onboarding.rendering import (
    render_provider_ready_config,
    rendered_default_config,
)
from crewplane.core.config import AgentConfig, Config, load_config
from crewplane.version import SCHEMA_VERSION


def test_generated_pi_profile_disables_automatic_extensions(tmp_path) -> None:
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        render_provider_ready_config(rendered_default_config(), ("pi",))
    )
    config = load_config(config_path)
    agent = config.agents["pi"]
    harness = tmp_path / "pi-double.py"
    harness.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "Path('arguments.json').write_text(json.dumps(sys.argv[1:]))\n"
        "print(sys.stdin.read(), end='')\n"
    )
    agent.cli_cmd = [sys.executable, str(harness)]
    output = tmp_path / "answer.md"

    asyncio.run(
        CliInvokerAdapter()
        .create_invoker(config)
        .invoke(agent, None, "prompt", output, tmp_path)
    )

    arguments = json.loads((tmp_path / "arguments.json").read_text())
    assert "--no-extensions" in arguments
    assert not {"--no-tools", "--no-builtin-tools", "--extension", "-e"} & set(
        arguments
    )
    assert output.read_text() == "prompt"


def test_pi_subprocess_receives_literal_stdin_and_publishes_only_stdout(
    tmp_path,
) -> None:
    harness = tmp_path / "pi-double.py"
    harness.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "import time\n"
        "time.sleep(0.05)\n"
        "prompt = sys.stdin.read()\n"
        "Path('captured.json').write_text(json.dumps([sys.argv[1:], prompt]))\n"
        "print('diagnostic', file=sys.stderr)\n"
        "print(prompt, end='')\n"
    )
    config = AgentConfig(
        cli_cmd=[sys.executable, str(harness)],
        provider_kind="pi",
        invocation_idle_timeout_seconds=0.01,
        extra_args=[
            "--no-extensions",
            "--extension",
            "local.ts",
            "--tools",
            "read,bash",
        ],
    )
    prompt = "--help\nλ \"quote\" 'single' $SHELL"
    output = tmp_path / "answer.md"
    usages = []
    context = InvocationContext(
        "node", "task", "pi", "executor", usage_recorder=usages.append
    )
    asyncio.run(
        CliInvokerAdapter()
        .create_invoker(Config(version=SCHEMA_VERSION, agents={"pi": config}))
        .invoke(
            config, "native:model", prompt, output, tmp_path, invocation_context=context
        )
    )
    arguments, received_prompt = json.loads((tmp_path / "captured.json").read_text())
    assert received_prompt == prompt
    assert arguments == [
        "--model",
        "native:model",
        *config.extra_args,
        "--print",
        "--mode",
        "text",
        "--no-session",
        "--approve",
    ]
    assert output.read_text() == prompt
    assert usages[0].provider_usage_status == "none"
    assert usages[0].visible_estimate_is_lower_bound


@pytest.mark.parametrize("exit_code, stdout", [(0, " \n"), (7, "partial answer")])
def test_pi_never_publishes_blank_or_failed_answers(
    tmp_path, exit_code, stdout
) -> None:
    harness = tmp_path / "pi-double.py"
    harness.write_text(
        "import sys\n"
        f"print({stdout!r})\n"
        "print('permission denied', file=sys.stderr)\n"
        f"sys.exit({exit_code})\n"
    )
    config = AgentConfig(cli_cmd=[sys.executable, str(harness)], provider_kind="pi")
    output = tmp_path / "answer.md"
    with pytest.raises(RuntimeError):
        asyncio.run(
            CliInvokerAdapter()
            .create_invoker(Config(version=SCHEMA_VERSION, agents={"pi": config}))
            .invoke(config, None, "prompt", output, tmp_path)
        )
    assert not output.exists()


def test_pi_selected_answer_uses_configured_retries(tmp_path) -> None:
    harness = tmp_path / "pi-double.py"
    harness.write_text(
        "from pathlib import Path\n"
        "marker = Path('attempt')\n"
        "print('answer' if marker.exists() else 'retry this')\n"
        "marker.write_text('called')\n"
    )
    config = AgentConfig(
        cli_cmd=[sys.executable, str(harness)],
        provider_kind="pi",
        retry_on_output_contains=["retry this"],
        retry_delay_seconds=0,
        max_retries=1,
    )
    output = tmp_path / "answer.md"
    asyncio.run(
        CliInvokerAdapter()
        .create_invoker(Config(version=SCHEMA_VERSION, agents={"pi": config}))
        .invoke(config, None, "prompt", output, tmp_path)
    )
    assert output.read_text() == "answer\n"

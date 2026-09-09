from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from crewplane.adapters.invokers.cli import CliInvokerAdapter
from crewplane.adapters.invokers.cli_invoker import build_cli_invocation_plan
from crewplane.architecture.contracts import PromptTransport
from crewplane.core.config import AgentConfig, Config
from crewplane.version import SCHEMA_VERSION


@pytest.mark.parametrize(
    "prompt",
    [
        pytest.param("First line\nSecond line\n", id="multiline"),
        pytest.param(
            "Review 'quoted' and \"double quoted\" $HOME `literal`", id="quotes"
        ),
        pytest.param("Review café, 日本語 and 🛫", id="unicode"),
        pytest.param(
            "请审查这段代码，检查错误并提出改进建议。", id="simplified-chinese"
        ),
        pytest.param(
            "請審查這段程式碼，檢查錯誤並提出改善建議。", id="traditional-chinese"
        ),
        pytest.param(
            "请按以下步骤审查：\n一、检查输入验证。\n二、确认错误处理。\n",
            id="chinese-multiline",
        ),
        pytest.param(
            "检查 API 的 UTF-8 输出；保留“中文”、'引号'、\"quotes\"、$HOME 和 `代码`。🛫",
            id="chinese-mixed-punctuation",
        ),
    ],
)
@pytest.mark.parametrize("transport", ["argv", "stdin"])
def test_prompt_survives_plan_and_subprocess_transport(
    tmp_path: Path,
    prompt: str,
    transport: PromptTransport,
) -> None:
    program = (
        "import json, sys; "
        "payload = {'argv': sys.argv[1:], "
        "'stdin': sys.stdin.buffer.read().decode('utf-8')}; "
        "sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))"
    )
    agent = AgentConfig(
        cli_cmd=[sys.executable, "-c", program],
        prompt_transport=transport,
        prompt_transport_arg="--prompt" if transport == "argv" else "-",
        model_arg="--model",
        extra_args=["--mode", "review"],
    )
    output_file = tmp_path / "output.md"
    expected_arguments = ["--model", "model-a", "--mode", "review"]
    expected_arguments += ["--prompt", prompt] if transport == "argv" else ["-"]
    expected_stdin = prompt if transport == "stdin" else ""

    plan = build_cli_invocation_plan(agent, "model-a", prompt, output_file)

    assert plan.cmd[3:] == expected_arguments
    assert plan.stdin_data == (prompt.encode("utf-8") if transport == "stdin" else None)
    config = Config(version=SCHEMA_VERSION, agents={"local": agent})
    invoker = CliInvokerAdapter().create_invoker(config)
    asyncio.run(invoker.invoke(agent, "model-a", prompt, output_file, tmp_path))

    result = json.loads(output_file.read_text(encoding="utf-8"))
    assert result == {"argv": expected_arguments, "stdin": expected_stdin}

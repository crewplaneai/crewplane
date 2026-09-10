from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from crewplane.cli.app import app
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.version import SCHEMA_VERSION


@pytest.mark.parametrize(
    ("instruction", "filename", "context"),
    [
        pytest.param(
            "请根据以下需求审查代码，保留原文标点：",
            "需求 说明.md",
            "支持中文搜索。\n输入“订单编号”后，显示对应的订单。\n",
            id="simplified-chinese",
        ),
        pytest.param(
            "請根據以下需求審查程式碼，保留原文標點：",
            "需求 說明.md",
            "支援中文搜尋。\n輸入「訂單編號」後，顯示對應的訂單。\n",
            id="traditional-chinese",
        ),
    ],
)
def test_chinese_workflow_prompt_and_file_contents_survive_artifact_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    instruction: str,
    filename: str,
    context: str,
) -> None:
    state_root = tmp_path / ".crewplane"
    workflow_root = state_root / "workflows"
    workflow_root.mkdir(parents=True)
    (tmp_path / filename).write_text(context, encoding="utf-8")
    (workflow_root / "chinese.task.md").write_text(
        f"""---
schema_version: '{SCHEMA_VERSION}'
name: Chinese Prompts
nodes:
  - id: inspect
    mode: sequential
    providers: [local]
---
## inspect
{instruction}

{{{{file:{filename}}}}}
""",
        encoding="utf-8",
    )
    config = Config(
        version=SCHEMA_VERSION,
        agents={"local": AgentConfig(cli_cmd=["__provider_must_not_run__"])},
        settings=Settings(
            integrations={
                "invoker": {
                    "implementation": "mock",
                    "options": {"output_mode": "echo", "observation_delay_seconds": 0},
                },
                "ui": {"implementation": "none"},
            }
        ),
    )
    (state_root / "config.yml").write_text(config.model_dump_json(), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(app, ["run", "--no-live"])

    assert result.exit_code == 0, result.output
    (provider_output,) = (state_root / "execution-stages").glob("*/inspect/*_round1.md")
    (final_output,) = (state_root / "execution-results").glob("*/inspect-result.md")
    for path in (provider_output, final_output):
        payload = path.read_bytes()
        assert instruction.encode("utf-8") in payload
        assert context.encode("utf-8") in payload
        assert b"{{file:" not in payload
    assert (tmp_path / filename).read_text(encoding="utf-8") == context

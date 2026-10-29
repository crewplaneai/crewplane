import re
from pathlib import Path

from rich.console import Console

from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.preflight import PreflightCompileOptions, compile_preflight_preview
from crewplane.core.preflight.source import load_workflow_source_for_preflight


def test_documented_folder_and_chunk_workflows_compile_with_mock(
    tmp_path: Path,
) -> None:
    examples = (Path(__file__).parents[4] / "docs/examples/composition.md").read_text(
        encoding="utf-8"
    )
    documents = re.findall(
        r"`([^`]+\.task\.md)`:\n\n```yaml\n(.*?)```", examples, re.DOTALL
    )
    assert len(documents) == 4
    directory = tmp_path / ".crewplane/workflows"
    directory.mkdir(parents=True)
    for name, content in documents:
        (directory / name).write_text(content, encoding="utf-8", newline="\n")
    (tmp_path / "request.md").write_bytes(b"Analyze the two supplied text excerpts.\n")
    config = Config(
        version="1.0",
        agents={"codex": AgentConfig(cli_cmd=["mock"])},
        settings=Settings(
            max_concurrent_nodes=4, integrations={"invoker": {"implementation": "mock"}}
        ),
    )
    snapshot = build_runtime_config_snapshot(config, Console(), no_live=True).snapshot
    for name in ("folder-audit.task.md", "chunk-analysis.task.md"):
        source = load_workflow_source_for_preflight(directory / name, tmp_path)
        preview = compile_preflight_preview(
            source,
            config,
            snapshot,
            PreflightCompileOptions(tmp_path, tmp_path / ".crewplane"),
        )
        assert not preview.has_errors(), preview.diagnostics
        assert len(preview.foreach_templates) == 1
    assert not (tmp_path / ".crewplane/execution-stages").exists()

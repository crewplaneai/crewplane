import asyncio
import io
import json
import os
import sys
from pathlib import Path

import pytest
from rich.console import Console

from crewplane.architecture.contracts import JsonObject
from crewplane.core.config import AgentConfig, Config
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.version import SCHEMA_VERSION

FIXTURE_PACKAGE = (
    Path(__file__).resolve().parents[1] / "fixtures/packaging/cli_provider_plugin"
)
OLLAMA_PACKAGE = Path(__file__).resolve().parents[2] / "plugins/crewplane-ollama"


def expose_distribution(
    monkeypatch: pytest.MonkeyPatch, root: Path, package: Path, name: str, version: str
) -> Path:
    """Expose source and test-owned metadata without installing into the checkout."""
    info = root / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    metadata = info / "METADATA"
    metadata.write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    monkeypatch.syspath_prepend(str(package / "src"))
    monkeypatch.syspath_prepend(str(root))
    return metadata


def fixture_options() -> JsonObject:
    return {
        "providers": {
            "fixture": {
                "implementation": "crewplane_fixture_provider:FIXTURE",
                "distribution": "crewplane-fixture-provider",
            }
        }
    }


def fake_ollama(monkeypatch: pytest.MonkeyPatch, root: Path) -> tuple[Path, Path]:
    if os.name != "posix":
        pytest.skip("Fake Ollama executable requires POSIX shebang support")
    program = Path(__file__).resolve().parents[1] / "fixtures/providers/ollama_cli.py"
    executable = root / "ollama"
    executable.write_text(
        f"#!{sys.executable}\n" + program.read_text(), encoding="utf-8"
    )
    executable.chmod(0o755)
    receipts = root / "receipts"
    receipts.mkdir()
    monkeypatch.setenv("CREWPLANE_FAKE_ROOT", str(receipts))
    return executable, receipts


async def wait_for_receipt(root: Path, pattern: str) -> Path:
    async with asyncio.timeout(10):
        while not (paths := list(root.glob(pattern))):
            await asyncio.sleep(0.01)
    return paths[0]


def command_receipts(root: Path, phase: str) -> list[dict[str, object]]:
    return [
        json.loads(path.read_text()) for path in sorted(root.glob(f"{phase}-*.json"))
    ]


def provider_config(agent: AgentConfig, options: JsonObject) -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={"local": agent},
        settings={
            "integrations": {
                "invoker": {"implementation": "cli", "options": options},
                "ui": {"implementation": "none"},
            }
        },
    )


def provider_source(
    model: str | None = None, downstream: bool = False
) -> PreflightWorkflowSource:
    nodes = [
        WorkflowNode(
            id="upstream",
            mode="sequential",
            providers=[ProviderSpec(provider="local", model=model)],
            prompt_segments=[PromptSegment(content="hello", role="shared")],
        )
    ]
    if downstream:
        nodes.append(
            WorkflowNode(
                id="downstream",
                mode="sequential",
                needs=["upstream"],
                providers=[ProviderSpec(provider="local")],
                prompt_segments=[
                    PromptSegment(
                        content="Summarize {{upstream.output}}", role="shared"
                    )
                ],
            )
        )
    return PreflightWorkflowSource.from_workflow(
        WorkflowPlan(name="plugin-flow", nodes=nodes)
    )


def quiet_console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=120)

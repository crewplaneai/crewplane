import asyncio
from pathlib import Path

import pytest

from crewplane.core.config import AgentConfig
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    OptionalOutputInvoker,
)


@pytest.mark.parametrize("invoker_type", [MockAgentInvoker, OptionalOutputInvoker])
def test_scripted_invoker_rejects_calls_after_exhaustion(
    tmp_path: Path, invoker_type
) -> None:
    invoker = invoker_type(outputs=["expected output"])
    output = tmp_path / "output.md"

    async def invoke() -> None:
        await invoker.invoke(
            AgentConfig(cli_cmd=["unused"]), "test", "prompt", output, tmp_path
        )

    asyncio.run(invoke())
    assert output.read_text() == "expected output"
    with pytest.raises(AssertionError, match="Unexpected invocation 2"):
        asyncio.run(invoke())
    assert output.read_text() == "expected output"


def test_empty_script_does_not_enable_unscripted_mode(tmp_path: Path) -> None:
    invoker = MockAgentInvoker(outputs=[])
    with pytest.raises(AssertionError, match="Unexpected invocation 1"):
        asyncio.run(
            invoker.invoke(
                AgentConfig(cli_cmd=["unused"]),
                "test",
                "prompt",
                tmp_path / "output.md",
                tmp_path,
            )
        )
    assert not (tmp_path / "output.md").exists()

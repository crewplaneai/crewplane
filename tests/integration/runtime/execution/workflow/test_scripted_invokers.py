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


@pytest.mark.parametrize(
    "field", ["node_id", "task_id", "audit_round_num", "round_num", "attempt_num"]
)
def test_identity_script_distinguishes_every_repeated_invocation(
    tmp_path: Path, field: str
) -> None:
    from dataclasses import replace

    from crewplane.architecture.contracts import InvocationContext
    from crewplane.core.workflow.keywords import ProviderRole
    from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
        InvocationKey,
        ScriptedAgentInvoker,
    )

    first = InvocationKey("node", "task", 1, 1, 1)
    second = replace(
        first, **{field: "other" if field in ("node_id", "task_id") else 2}
    )
    invoker = ScriptedAgentInvoker({first: "first response", second: "second response"})
    output = tmp_path / "output.md"

    async def invoke(key):
        context = InvocationContext(
            node_id=key.node_id,
            task_id=key.task_id,
            provider="alpha",
            role=ProviderRole.EXECUTOR,
            audit_round_num=key.audit_round_num,
            round_num=key.round_num,
            attempt_num=key.attempt_num,
            findings_enabled=False,
        )
        await invoker.invoke(
            AgentConfig(cli_cmd=["unused"]),
            "test",
            "prompt",
            output,
            tmp_path,
            invocation_context=context,
        )

    asyncio.run(invoke(second))
    assert output.read_text() == "second response"
    with pytest.raises(AssertionError, match="Unconsumed"):
        invoker.assert_consumed()
    with pytest.raises(AssertionError, match="Unexpected invocation"):
        asyncio.run(invoke(second))
    assert output.read_text() == "second response"
    asyncio.run(invoke(first))
    assert output.read_text() == "first response"
    invoker.assert_consumed()

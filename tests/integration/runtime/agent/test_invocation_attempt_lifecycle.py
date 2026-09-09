import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from crewplane.architecture.contracts import (
    CommandResult,
    InvocationContext,
    InvocationDiagnostic,
    InvocationPlan,
    InvocationUsage,
    OneShotFailureRetryPolicy,
    OutputExtractionResult,
    ProviderKind,
)
from crewplane.core.config import AgentConfig
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.failures import InvocationFailureError
from crewplane.runtime.agent.invocation.loop import run_invocation_loop


@pytest.fixture
def invocation_plan() -> InvocationPlan:
    return InvocationPlan(
        cmd=["provider"],
        stdin_data=b"prompt",
        structured_output_file=None,
        structured_output_mode="none",
        output_extractor=None,
        usage_decoder=None,
        quota_parser="generic",
        failure_profile=ProviderKind.GENERIC,
        log_header=b"",
        log_provider_kind=ProviderKind.GENERIC,
    )


@pytest.mark.parametrize("cancelled", [False, True])
def test_extractor_exception_cleans_attempt_streams_and_structured_output(
    tmp_path: Path, invocation_plan: InvocationPlan, cancelled: bool
) -> None:
    stdout_path = tmp_path / "stdout.capture"
    stderr_path = tmp_path / "stderr.capture"
    structured_path = tmp_path / "structured.txt"
    output_file = tmp_path / "output.txt"
    usages: list[InvocationUsage] = []
    failure = asyncio.CancelledError() if cancelled else RuntimeError("extract failed")

    def capture_result(**kwargs: object) -> CommandResult:
        assert kwargs["append_log"] is False
        stdout_path.write_text("response", encoding="utf-8")
        stderr_path.write_text("diagnostic", encoding="utf-8")
        structured_path.write_text("answer", encoding="utf-8")
        return CommandResult(0, "", "", stdout_path, stderr_path)

    def extract_output(
        result: CommandResult, structured_output_file: Path | None
    ) -> OutputExtractionResult:
        assert list(result.iter_stdout_lines()) == ["response"]
        assert list(result.iter_stderr_lines()) == ["diagnostic"]
        assert structured_output_file == structured_path
        assert structured_path.read_text(encoding="utf-8") == "answer"
        raise failure

    context = InvocationContext(
        node_id="node",
        task_id="provider_executor_0",
        provider="provider",
        role=ProviderRole.EXECUTOR,
        usage_recorder=usages.append,
    )
    plan = replace(
        invocation_plan,
        structured_output_file=structured_path,
        output_extractor=extract_output,
    )

    with pytest.raises(type(failure)) as caught:
        asyncio.run(
            run_invocation_loop(
                config=AgentConfig(cli_cmd=["provider"]),
                prompt="prompt",
                output_file=output_file,
                log_file=None,
                cwd=tmp_path,
                invocation_context=context,
                command_runner=AsyncMock(side_effect=capture_result),
                plan=plan,
            )
        )

    assert caught.value is failure
    assert not stdout_path.exists()
    assert not stderr_path.exists()
    assert not structured_path.exists()
    assert not output_file.exists()
    assert len(usages) == (0 if cancelled else 1)
    if not cancelled:
        assert usages[0].attempt_count == 1


@pytest.mark.parametrize("raw_stdout", ["provider wrapper metadata", "Quota reached."])
def test_quota_failure_preserves_extracted_non_quota_failure(
    tmp_path: Path, invocation_plan: InvocationPlan, raw_stdout: str
) -> None:
    extracted_failure = "temporary transport failure"

    def extract_output(
        result: CommandResult, structured_output_file: Path | None
    ) -> OutputExtractionResult:
        assert result.stdout_text == raw_stdout
        assert structured_output_file is None
        return OutputExtractionResult(
            output_text=extracted_failure,
            output_extraction_status="success",
        )

    runner = AsyncMock(
        side_effect=[
            CommandResult(0, raw_stdout, ""),
            CommandResult(1, "Quota reached.", ""),
            CommandResult(1, "Quota reached.", ""),
        ]
    )
    config = AgentConfig(
        cli_cmd=["provider"],
        max_retries=1,
        retry_on_output_contains=[extracted_failure],
        retry_delay_seconds=0,
        quota_reached_retry_delay_seconds=0,
        quota_retry_max_attempts=1,
    )

    with pytest.raises(InvocationFailureError, match="attempt ceiling of 1") as caught:
        asyncio.run(
            run_invocation_loop(
                config=config,
                prompt="prompt",
                output_file=tmp_path / "output.txt",
                log_file=None,
                cwd=tmp_path,
                invocation_context=None,
                command_runner=runner,
                plan=replace(invocation_plan, output_extractor=extract_output),
            )
        )

    assert runner.await_count == 3
    last_failure = caught.value.last_non_quota_failure
    assert last_failure is not None
    assert last_failure.message == extracted_failure


def test_mixed_retries_preserve_independent_budgets_and_last_failure(
    tmp_path: Path, invocation_plan: InvocationPlan
) -> None:
    quota = CommandResult(1, "Quota reached.", "")
    runner = AsyncMock(
        side_effect=[
            CommandResult(2, "", "first transport failure"),
            quota,
            CommandResult(1, "", "temporary capacity failure"),
            CommandResult(2, "", "last transport failure"),
            quota,
            quota,
        ]
    )
    usages: list[InvocationUsage] = []
    diagnostics: list[InvocationDiagnostic] = []
    context = InvocationContext(
        node_id="node",
        task_id="provider_executor_0",
        provider="provider",
        role=ProviderRole.EXECUTOR,
        usage_recorder=usages.append,
        diagnostics=diagnostics.append,
    )
    config = AgentConfig(
        cli_cmd=["provider"],
        max_retries=2,
        retry_on_exit_codes=[2],
        retry_delay_seconds=0,
        quota_reached_retry_delay_seconds=0,
        quota_retry_max_attempts=2,
    )
    plan = replace(
        invocation_plan,
        one_shot_failure_retry=OneShotFailureRetryPolicy(
            output_contains=("temporary capacity failure",),
            wait_seconds=0,
            reason="capacity",
            notice_message="Retrying temporary capacity failure",
        ),
    )
    output_file = tmp_path / "output.txt"

    with pytest.raises(InvocationFailureError, match="attempt ceiling of 2") as caught:
        asyncio.run(
            run_invocation_loop(
                config=config,
                prompt="prompt",
                output_file=output_file,
                log_file=None,
                cwd=tmp_path,
                invocation_context=context,
                command_runner=runner,
                plan=plan,
            )
        )

    assert runner.await_count == 6
    assert [
        call.kwargs["invocation_context"].attempt_num for call in runner.await_args_list
    ] == [1, 2, 3, 4, 5, 6]
    assert [diagnostic.operation for diagnostic in diagnostics] == [
        "retry_scheduled",
        "quota_retry_scheduled",
        "retry_scheduled",
        "retry_scheduled",
        "quota_retry_scheduled",
    ]
    last_failure = caught.value.last_non_quota_failure
    assert last_failure is not None
    assert last_failure.message == "last transport failure"
    assert len(usages) == 1
    assert usages[0].attempt_count == 6
    assert not output_file.exists()

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from crewplane.adapters.invokers.cli_invoker import get_cli_provider_capability
from crewplane.architecture.contracts import (
    CommandResult,
    InvocationContext,
    InvocationDiagnostic,
    InvocationPlan,
    InvocationUsage,
    OneShotFailureRetryPolicy,
    OutputExtractionResult,
    ProviderKind,
    ProviderTokenUsage,
    UsageDecodeResult,
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
        output_extractor=None,
        usage_decoder=None,
        quota_classifier=get_cli_provider_capability("generic").quota_classifier,
        failure_classifier=get_cli_provider_capability(
            ProviderKind.GENERIC
        ).failure_classifier,
        log_header=b"",
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "retry",
        "one_shot",
        "exhausted",
        "failed_exit",
        "terminal_quota",
        "selected_retry",
        "selected_exhausted",
    ],
)
@pytest.mark.parametrize("cancelled", [False, True])
def test_failure_classifier_borrows_attempt_streams_and_errors_release_outputs(
    tmp_path, invocation_plan, scenario, cancelled
) -> None:
    selected = scenario.startswith("selected")
    stdout = tmp_path / "stdout"
    stderr = tmp_path / "stderr"
    answer = tmp_path / "answer"
    failure = (
        asyncio.CancelledError() if cancelled else RuntimeError("classifier failed")
    )
    observed = []

    def capture(**kwargs):
        assert kwargs["append_log"] is False
        stdout.write_text(
            "quota reached" if scenario == "terminal_quota" else "raw retry"
        )
        stderr.write_text("original diagnostic")
        return CommandResult(0 if selected else 1, "", "", stdout, stderr)

    def extract(result, path):
        assert path is None
        assert list(result.iter_stdout_lines()) == ["raw retry"]
        answer.write_text("selected retry")
        return OutputExtractionResult("", "success", answer, 14, True)

    def classify(result):
        observed.append(
            (list(result.iter_stdout_lines()), list(result.iter_stderr_lines()))
        )
        raise failure

    config = AgentConfig(
        cli_cmd=["provider"],
        max_retries=1 if scenario.endswith("retry") else 0,
        retry_on_output_contains=["retry"] if scenario != "failed_exit" else [],
        retry_delay_seconds=0,
        quota_retry_max_wait_seconds=1,
    )
    plan = replace(
        invocation_plan,
        output_extractor=extract if selected else None,
        failure_classifier=classify,
        one_shot_failure_retry=OneShotFailureRetryPolicy(("retry",), 0, "test", "retry")
        if scenario == "one_shot"
        else None,
    )
    with pytest.raises(type(failure)) as caught:
        asyncio.run(
            run_invocation_loop(
                config,
                "prompt",
                tmp_path / "out",
                None,
                tmp_path,
                None,
                AsyncMock(side_effect=capture),
                plan,
            )
        )
    assert caught.value is failure
    assert observed == [
        (
            [
                "selected retry"
                if selected
                else "quota reached"
                if scenario == "terminal_quota"
                else "raw retry"
            ],
            ["original diagnostic"],
        )
    ]
    assert not any(path.exists() for path in (stdout, stderr, answer, tmp_path / "out"))


def test_generic_invocation_accepts_decoded_usage_from_original_streams(
    tmp_path, invocation_plan
) -> None:
    usages = []

    def decode(result):
        assert result.stdout_text == "original report"
        return UsageDecodeResult(
            ProviderTokenUsage(input=0, output=0, total=0), valid_report_count=1
        )

    def extract(result, path):
        assert result.stdout_text == "original report"
        assert path is None
        return OutputExtractionResult("answer", "success")

    context = InvocationContext(
        "node", "task", "generic", "executor", usage_recorder=usages.append
    )
    asyncio.run(
        run_invocation_loop(
            AgentConfig(cli_cmd=["provider"], provider_kind="generic"),
            "prompt",
            tmp_path / "out",
            None,
            tmp_path,
            context,
            AsyncMock(return_value=CommandResult(0, "original report", "")),
            replace(invocation_plan, usage_decoder=decode, output_extractor=extract),
        )
    )
    assert (tmp_path / "out").read_text() == "answer"
    assert len(usages) == 1
    assert usages[0].provider_usage_status == "full"
    assert usages[0].provider_tokens["total"] == 0
    assert usages[0].visible_estimate_is_lower_bound


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


def test_quota_classifier_exception_releases_failed_extraction_and_raw_captures(
    tmp_path, invocation_plan
) -> None:
    stdout = tmp_path / "stdout"
    stderr = tmp_path / "stderr"
    extracted = tmp_path / "extracted"

    def capture(**kwargs):
        assert kwargs["append_log"] is False
        stdout.write_text("original")
        stderr.write_text("diagnostic")
        return CommandResult(0, "", "", stdout, stderr)

    def extract(result, path):
        assert list(result.iter_stdout_lines()) == ["original"]
        assert path is None
        extracted.write_text("incomplete")
        return OutputExtractionResult("", "malformed", extracted, 10, True)

    def classify(result, hints, now):
        assert list(result.iter_stdout_lines()) == ["original"]
        assert list(result.iter_stderr_lines()) == ["diagnostic"]
        assert hints == ("custom quota",)
        assert now.tzinfo is not None
        assert not extracted.exists()
        raise RuntimeError("quota classifier failed")

    with pytest.raises(RuntimeError, match="quota classifier failed"):
        asyncio.run(
            run_invocation_loop(
                AgentConfig(
                    cli_cmd=["provider"], quota_reached_on_contains=["custom quota"]
                ),
                "prompt",
                tmp_path / "out",
                None,
                tmp_path,
                None,
                AsyncMock(side_effect=capture),
                replace(
                    invocation_plan, output_extractor=extract, quota_classifier=classify
                ),
            )
        )
    assert not any(
        path.exists() for path in (stdout, stderr, extracted, tmp_path / "out")
    )

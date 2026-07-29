import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from crewplane.adapters.invokers.cli_invoker.capabilities import (
    CODEX_MODEL_CAPACITY_MESSAGE,
    CODEX_MODEL_CAPACITY_RETRY_DELAY_SECONDS,
    CODEX_MODEL_CAPACITY_RETRY_POLICY,
)
from crewplane.architecture.contracts import CommandResult
from crewplane.core.config import AgentConfig
from crewplane.runtime.agent.invocation.retry import (
    NoFailureRetry,
    NoQuotaRetry,
    QuotaRetryFailure,
    ScheduleFailureRetry,
    ScheduleQuotaRetry,
    evaluate_failure_retry,
    evaluate_quota_retry,
)


class FixedQuotaRetryDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = datetime(2026, 6, 7, 6, 43, 13, tzinfo=UTC)
        if tz is None:
            return fixed.replace(tzinfo=None)
        return fixed.astimezone(tz)


@contextmanager
def local_timezone(name: str) -> Iterator[None]:
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    if hasattr(time, "tzset"):
        time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        if hasattr(time, "tzset"):
            time.tzset()


def test_evaluate_failure_retry_schedules_retry_notice() -> None:
    decision = evaluate_failure_retry(
        config=AgentConfig(
            cli_cmd=["tool"],
            default_model="test",
            max_retries=1,
            retry_delay_seconds=2,
            retry_on_exit_codes=[2],
        ),
        cmd=["tool"],
        result=CommandResult(returncode=2, stdout_text="", stderr_text="failed"),
        retry_count=0,
    )

    assert isinstance(decision, ScheduleFailureRetry)
    assert decision.retry_count == 1
    assert decision.wait_seconds == 2
    assert decision.notice.operation == "retry_scheduled"


def test_evaluate_failure_retry_reports_exhausted_match_without_scheduling() -> None:
    decision = evaluate_failure_retry(
        config=AgentConfig(
            cli_cmd=["tool"],
            default_model="test",
            max_retries=0,
            retry_on_output_contains=["retry"],
        ),
        cmd=["tool"],
        result=CommandResult(returncode=0, stdout_text="retry", stderr_text=""),
        retry_count=0,
    )

    assert isinstance(decision, NoFailureRetry)
    assert decision.retry_matched is True
    assert decision.retry_count == 0
    assert decision.reports_retry_exhaustion_for_failed_exit is False


def test_codex_model_capacity_failure_schedules_built_in_retry() -> None:
    assert CODEX_MODEL_CAPACITY_MESSAGE == (
        "Selected model is at capacity. Please try a different model."
    )
    assert CODEX_MODEL_CAPACITY_RETRY_DELAY_SECONDS == 5.0

    decision = evaluate_failure_retry(
        config=AgentConfig(
            cli_cmd=["codex", "exec"],
            provider_kind="codex",
            max_retries=0,
        ),
        cmd=["codex", "exec"],
        result=CommandResult(
            returncode=1,
            stdout_text=CODEX_MODEL_CAPACITY_MESSAGE,
            stderr_text="",
        ),
        retry_count=0,
        one_shot_failure_retry=CODEX_MODEL_CAPACITY_RETRY_POLICY,
    )

    assert isinstance(decision, ScheduleFailureRetry)
    assert decision.retry_count == 0
    assert decision.wait_seconds == 5.0
    assert decision.notice.operation == "retry_scheduled"
    assert CODEX_MODEL_CAPACITY_MESSAGE in decision.notice.message
    assert "five seconds" in decision.notice.message
    assert "built-in attempt 1/1" in decision.notice.message
    attributes = decision.notice.attributes
    assert attributes is not None
    assert attributes["reason"] == "codex_model_capacity"
    assert attributes["built_in"] is True
    assert attributes["retry_count"] == 1
    assert attributes["max_retries"] == 1
    assert attributes["retry_delay_seconds"] == 5.0


def test_codex_capacity_matches_decorated_case_insensitive_output() -> None:
    decision = evaluate_failure_retry(
        config=AgentConfig(
            cli_cmd=["codex", "exec"],
            provider_kind="codex",
            max_retries=0,
        ),
        cmd=["codex", "exec"],
        result=CommandResult(
            returncode=1,
            stdout_text="",
            stderr_text=f"error: {CODEX_MODEL_CAPACITY_MESSAGE.swapcase()} [request]",
        ),
        retry_count=0,
        one_shot_failure_retry=CODEX_MODEL_CAPACITY_RETRY_POLICY,
    )

    assert isinstance(decision, ScheduleFailureRetry)
    assert decision.wait_seconds == 5.0


def test_codex_model_capacity_failure_reports_exhausted_after_built_in_retry() -> None:
    decision = evaluate_failure_retry(
        config=AgentConfig(
            cli_cmd=["codex", "exec"],
            provider_kind="codex",
            max_retries=0,
        ),
        cmd=["codex", "exec"],
        result=CommandResult(
            returncode=1,
            stdout_text="",
            stderr_text=CODEX_MODEL_CAPACITY_MESSAGE,
        ),
        retry_count=0,
        built_in_retry_used=True,
        one_shot_failure_retry=CODEX_MODEL_CAPACITY_RETRY_POLICY,
    )

    assert isinstance(decision, NoFailureRetry)
    assert decision.retry_count == 1
    assert decision.retry_matched is True
    assert decision.reports_retry_exhaustion_for_failed_exit is True


def test_codex_model_capacity_failure_retries_after_ordinary_retry() -> None:
    decision = evaluate_failure_retry(
        config=AgentConfig(
            cli_cmd=["codex", "exec"],
            provider_kind="codex",
            max_retries=3,
            retry_on_exit_codes=[2],
        ),
        cmd=["codex", "exec"],
        result=CommandResult(
            returncode=1,
            stdout_text="",
            stderr_text=CODEX_MODEL_CAPACITY_MESSAGE,
        ),
        retry_count=1,
        one_shot_failure_retry=CODEX_MODEL_CAPACITY_RETRY_POLICY,
    )

    assert isinstance(decision, ScheduleFailureRetry)
    assert decision.retry_count == 1
    assert decision.wait_seconds == 5.0
    attributes = decision.notice.attributes
    assert attributes is not None
    assert attributes["built_in"] is True
    assert attributes["reason"] == "codex_model_capacity"


def test_codex_provider_kind_does_not_enable_adapter_retry_policy() -> None:
    decision = evaluate_failure_retry(
        config=AgentConfig(
            cli_cmd=["codex", "exec"],
            provider_kind="codex",
            max_retries=0,
        ),
        cmd=["codex", "exec"],
        result=CommandResult(
            returncode=1,
            stdout_text="",
            stderr_text=CODEX_MODEL_CAPACITY_MESSAGE,
        ),
        retry_count=0,
    )

    assert isinstance(decision, NoFailureRetry)
    assert decision.retry_matched is False


def test_codex_model_capacity_rule_ignores_successful_quoted_message() -> None:
    decision = evaluate_failure_retry(
        config=AgentConfig(
            cli_cmd=["codex", "exec"],
            provider_kind="codex",
            max_retries=0,
        ),
        cmd=["codex", "exec"],
        result=CommandResult(
            returncode=0,
            stdout_text=f"Quoted provider error: {CODEX_MODEL_CAPACITY_MESSAGE}",
            stderr_text="",
        ),
        retry_count=0,
        one_shot_failure_retry=CODEX_MODEL_CAPACITY_RETRY_POLICY,
    )

    assert isinstance(decision, NoFailureRetry)
    assert decision.retry_matched is False


def test_codex_model_capacity_rule_requires_complete_canonical_wording() -> None:
    decision = evaluate_failure_retry(
        config=AgentConfig(
            cli_cmd=["codex", "exec"],
            provider_kind="codex",
            max_retries=0,
        ),
        cmd=["codex", "exec"],
        result=CommandResult(
            returncode=1,
            stdout_text="",
            stderr_text="Selected model is at capacity. Please try a different model",
        ),
        retry_count=0,
        one_shot_failure_retry=CODEX_MODEL_CAPACITY_RETRY_POLICY,
    )

    assert isinstance(decision, NoFailureRetry)
    assert decision.retry_matched is False


def test_codex_capacity_retry_takes_precedence_over_explicit_retry_rule() -> None:
    decision = evaluate_failure_retry(
        config=AgentConfig(
            cli_cmd=["codex", "exec"],
            provider_kind="codex",
            max_retries=3,
            retry_delay_seconds=17,
            retry_on_output_contains=[CODEX_MODEL_CAPACITY_MESSAGE],
        ),
        cmd=["codex", "exec"],
        result=CommandResult(
            returncode=1,
            stdout_text="",
            stderr_text=CODEX_MODEL_CAPACITY_MESSAGE,
        ),
        retry_count=0,
        one_shot_failure_retry=CODEX_MODEL_CAPACITY_RETRY_POLICY,
    )

    assert isinstance(decision, ScheduleFailureRetry)
    assert decision.retry_count == 0
    assert decision.wait_seconds == 5.0
    attributes = decision.notice.attributes
    assert attributes is not None
    assert attributes["max_retries"] == 1
    assert attributes["built_in"] is True
    assert attributes["reason"] == "codex_model_capacity"


def test_codex_model_capacity_failure_bypasses_quota_retry() -> None:
    decision = evaluate_quota_retry(
        config=AgentConfig(
            cli_cmd=["codex", "exec"],
            provider_kind="codex",
            quota_reached_on_contains=[CODEX_MODEL_CAPACITY_MESSAGE],
        ),
        cmd=["codex", "exec"],
        quota_parser="codex",
        result=CommandResult(
            returncode=1,
            stdout_text="",
            stderr_text=CODEX_MODEL_CAPACITY_MESSAGE,
        ),
        quota_retry_started_at=123.0,
        quota_retry_count=2,
        one_shot_failure_retry=CODEX_MODEL_CAPACITY_RETRY_POLICY,
    )

    assert isinstance(decision, NoQuotaRetry)
    assert decision.quota_retry_started_at == 123.0
    assert decision.quota_retry_count == 2


def test_evaluate_quota_retry_schedules_retry_with_parsed_reset() -> None:
    decision = evaluate_quota_retry(
        config=AgentConfig(
            cli_cmd=["gemini"],
            provider_kind="gemini",
            default_model="test",
            quota_reached_retry_delay_seconds=0,
            quota_reset_sleep_floor_seconds=5,
        ),
        cmd=["gemini"],
        quota_parser="gemini",
        result=CommandResult(
            returncode=0,
            stdout_text=(
                "You have exhausted your capacity on this model. "
                "Your quota will reset after 2s."
            ),
            stderr_text="",
        ),
        quota_retry_started_at=None,
        quota_retry_count=0,
    )

    assert isinstance(decision, ScheduleQuotaRetry)
    assert decision.quota_retry_count == 1
    assert decision.wait_seconds >= 7.0
    assert decision.notice.operation == "quota_retry_scheduled"


def test_evaluate_quota_retry_returns_failure_when_reset_exceeds_guard() -> None:
    decision = evaluate_quota_retry(
        config=AgentConfig(
            cli_cmd=["gemini"],
            provider_kind="gemini",
            default_model="test",
        ),
        cmd=["gemini"],
        quota_parser="gemini",
        result=CommandResult(
            returncode=0,
            stdout_text=(
                "You have exhausted your capacity on this model. "
                "Your quota will reset after 6h."
            ),
            stderr_text="",
        ),
        quota_retry_started_at=None,
        quota_retry_count=0,
    )

    assert isinstance(decision, QuotaRetryFailure)
    assert "exceeds 5 hours" in decision.message


def test_evaluate_quota_retry_enforces_configured_attempt_ceiling() -> None:
    decision = evaluate_quota_retry(
        config=AgentConfig(
            cli_cmd=["gemini"],
            provider_kind="gemini",
            quota_retry_max_attempts=2,
        ),
        cmd=["gemini"],
        quota_parser="gemini",
        result=CommandResult(
            returncode=1,
            stdout_text="You have exhausted your capacity on this model.",
            stderr_text="",
        ),
        quota_retry_started_at=None,
        quota_retry_count=2,
    )

    assert isinstance(decision, QuotaRetryFailure)
    assert "configured attempt ceiling of 2" in decision.message


def test_evaluate_quota_retry_enforces_configured_wait_ceiling() -> None:
    decision = evaluate_quota_retry(
        config=AgentConfig(
            cli_cmd=["gemini"],
            provider_kind="gemini",
            quota_reached_retry_delay_seconds=120,
            quota_retry_max_wait_seconds=60,
        ),
        cmd=["gemini"],
        quota_parser="gemini",
        result=CommandResult(
            returncode=1,
            stdout_text="You have exhausted your capacity on this model.",
            stderr_text="",
        ),
        quota_retry_started_at=None,
        quota_retry_count=0,
    )

    assert isinstance(decision, QuotaRetryFailure)
    assert "configured wait ceiling of 1m" in decision.message


def test_quota_wait_ceiling_excludes_provider_execution_time() -> None:
    with patch(
        "crewplane.runtime.agent.invocation.retry.time.monotonic",
        return_value=20,
    ):
        decision = evaluate_quota_retry(
            config=AgentConfig(
                cli_cmd=["gemini"],
                provider_kind="gemini",
                quota_reached_retry_delay_seconds=10,
                quota_retry_max_wait_seconds=25,
            ),
            cmd=["gemini"],
            quota_parser="gemini",
            result=CommandResult(
                returncode=1,
                stdout_text="You have exhausted your capacity on this model.",
                stderr_text="",
            ),
            quota_retry_started_at=0,
            quota_retry_count=1,
            quota_retry_wait_seconds=10,
        )

    assert isinstance(decision, ScheduleQuotaRetry)


def test_bare_local_reset_uses_configured_delay_in_utc_timezone() -> None:
    with local_timezone("UTC"):
        decision = evaluate_quota_retry(
            config=AgentConfig(
                cli_cmd=["gemini"],
                provider_kind="gemini",
                quota_reached_retry_delay_seconds=73,
            ),
            cmd=["gemini"],
            quota_parser="gemini",
            result=CommandResult(
                returncode=1,
                stdout_text=(
                    "You have exhausted your capacity on this model. Try again at 2 PM."
                ),
                stderr_text="",
            ),
            quota_retry_started_at=None,
            quota_retry_count=0,
        )

    assert isinstance(decision, ScheduleQuotaRetry)
    assert decision.wait_seconds == 73
    assert "configured fixed delay" in decision.notice.message


def test_evaluate_failure_retry_reads_retried_output_from_persisted_stream() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = Path(tmp_dir) / "stream.txt"
        path.write_text(
            "\n".join(["noise"] * 500 + ["temporary output retry marker"]),
            encoding="utf-8",
        )
        decision = evaluate_failure_retry(
            config=AgentConfig(
                cli_cmd=["tool"],
                default_model="test",
                max_retries=1,
                retry_delay_seconds=0,
                retry_on_output_contains=["retry marker"],
            ),
            cmd=["tool"],
            result=CommandResult(
                returncode=0,
                stdout_text="",
                stderr_text="",
                stdout_path=path,
            ),
            retry_count=0,
        )

        assert isinstance(decision, ScheduleFailureRetry)
        assert decision.retry_count == 1
        attributes = decision.notice.attributes
        assert attributes is not None
        assert attributes["retry_count"] == 1


def test_codex_usage_limit_with_local_reset_schedules_quota_retry() -> None:
    with (
        patch(
            "crewplane.runtime.agent.quota.classifier.datetime",
            FixedQuotaRetryDateTime,
        ),
        local_timezone("America/Vancouver"),
    ):
        decision = evaluate_quota_retry(
            config=AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="test",
                quota_reached_on_contains=[
                    "usage limit",
                    "rate limit",
                    "try again in",
                ],
                quota_reached_retry_delay_seconds=0,
                quota_reset_sleep_floor_seconds=5,
            ),
            cmd=["codex", "exec"],
            quota_parser="codex",
            result=CommandResult(
                returncode=1,
                stdout_text=(
                    '{"type":"error","message":"You\'ve hit your usage limit for '
                    "GPT-5.3-Codex-Spark. Switch to another model now, or try "
                    'again at Jun 7th, 2026 2:18 AM."}'
                ),
                stderr_text="",
            ),
            quota_retry_started_at=None,
            quota_retry_count=0,
        )

    assert isinstance(decision, ScheduleQuotaRetry)
    assert decision.wait_seconds == 9292


def test_evaluate_quota_retry_reads_retried_quota_marker_from_persisted_stream() -> (
    None
):
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = Path(tmp_dir) / "quota.log"
        path.write_text(
            "\n".join(
                ["noise"] * 500
                + [
                    "You have exhausted your capacity on this model. Your quota will reset after 2s."
                ]
            ),
            encoding="utf-8",
        )
        decision = evaluate_quota_retry(
            config=AgentConfig(
                cli_cmd=["gemini"],
                provider_kind="gemini",
                default_model="test",
                quota_reached_retry_delay_seconds=0,
                quota_reset_sleep_floor_seconds=0,
            ),
            cmd=["gemini"],
            quota_parser="gemini",
            result=CommandResult(
                returncode=0,
                stdout_text="",
                stderr_text="",
                stdout_path=path,
            ),
            quota_retry_started_at=None,
            quota_retry_count=0,
        )

        assert isinstance(decision, ScheduleQuotaRetry)
        assert decision.quota_retry_count == 1
        assert decision.notice.operation == "quota_retry_scheduled"

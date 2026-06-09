import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from orchestrator_cli.architecture.contracts import CommandResult
from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.runtime.agent.invocation.retry import (
    NoFailureRetry,
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
        assert decision.notice.attributes["retry_count"] == 1


def test_codex_usage_limit_with_local_reset_schedules_quota_retry() -> None:
    with (
        patch(
            "orchestrator_cli.runtime.agent.quota.classifier.datetime",
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

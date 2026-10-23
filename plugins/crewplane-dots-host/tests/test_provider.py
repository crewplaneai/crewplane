from datetime import UTC, datetime

import pytest
from crewplane_dots_host.provider import DOTS_HOST, build_command, validate_request

from crewplane.adapters.invokers.cli_invoker import CliInvocationRequest
from crewplane.adapters.invokers.cli_invoker.command_posix import PosixCommandStrategy
from crewplane.architecture.contracts import CommandResult
from crewplane.core.config import AgentConfig


def request(**overrides):
    config = AgentConfig(
        cli_cmd=["crewplane-dots-host", "--queue", "queue", "invoke"],
        provider_kind="dots_host",
        prompt_transport="stdin",
        extra_args=["--reasoning", "xhigh"],
    )
    return CliInvocationRequest(
        config=config,
        model="gpt-6.1-sol",
        command_strategy=PosixCommandStrategy(),
        **overrides,
    )


def test_successful_report_is_not_quota():
    result = CommandResult(
        returncode=0,
        stdout_text="Review: callers are rate limited; usage limit exceeded is an error message.",
        stderr_text="",
    )
    assert not DOTS_HOST.quota_classifier(
        result, ("rate limited",), datetime.now(UTC)
    ).is_quota


def test_exact_model_and_prompt():
    command = build_command(request(), "exact\nprompt")
    assert command.stdin_data == b"exact\nprompt"
    assert command.cmd[-4:] == ["--model", "gpt-6.1-sol", "--reasoning", "xhigh"]


def test_reasoning_conflict_rejected():
    with pytest.raises(ValueError):
        validate_request(request(requested_reasoning="high"))

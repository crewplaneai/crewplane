from dataclasses import replace

import pytest

from crewplane.adapters.invokers.cli_invoker.capability import CliInvocationRequest
from crewplane.adapters.invokers.cli_invoker.providers.pi import PI
from crewplane.architecture.contracts import CommandResult
from crewplane.core.config import AgentConfig


def request(**values) -> CliInvocationRequest:
    return CliInvocationRequest(
        AgentConfig(cli_cmd=["pi"], provider_kind="pi", **values), None
    )


def test_pi_preserves_native_configuration_and_literal_stdin(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")
    config = AgentConfig(
        cli_cmd=["env", "PI_CODING_AGENT_DIR=/native", "pi", "--extension", "local.ts"],
        provider_kind="pi",
        extra_args=["--tools", "read,bash", "--mode", "text", "--no-session"],
    )
    invocation = CliInvocationRequest(config, "native/provider:model:thinking")
    PI.validate_request(invocation)
    prompt = '--help\n"quotes" λ'
    command = PI.build_command(invocation, prompt)
    assert command.cmd == [
        *config.cli_cmd,
        "--model",
        "native/provider:model:thinking",
        *config.extra_args,
        "--print",
        "--mode",
        "text",
        "--no-session",
        "--approve",
    ]
    assert command.stdin_data == prompt.encode()
    assert command.structured_output_file is None
    assert PI.usage_decoder is None
    assert not PI.supports_output_idle_timeout
    assert PI.log_presentation_format == "plain"


@pytest.mark.parametrize(
    "arguments",
    [
        ["--mode", "json"],
        ["--mode=rpc"],
        ["--mode"],
        ["--session", "saved"],
        ["--session-id=x"],
        ["--session-dir", "sessions"],
        ["--continue"],
        ["-c"],
        ["--resume"],
        ["-r"],
        ["--fork"],
        ["--name", "saved"],
        ["-n", "saved"],
        ["--no-approve"],
        ["-na"],
        ["--"],
        ["--help"],
        ["-h"],
        ["--version"],
        ["-v"],
        ["--export", "saved"],
        ["--list-models"],
        ["--extension"],
        ["auth", "check"],
        ["config"],
        ["install", "package"],
    ],
)
@pytest.mark.parametrize("location", ["cli_cmd", "extra_args"])
def test_pi_rejects_conflicting_configured_arguments(arguments, location) -> None:
    values = {location: arguments if location == "extra_args" else ["pi", *arguments]}
    invocation = CliInvocationRequest(
        AgentConfig(provider_kind="pi", **({"cli_cmd": ["pi"]} | values)), None
    )
    with pytest.raises(ValueError, match="Pi"):
        PI.validate_request(invocation)


@pytest.mark.parametrize(
    "values",
    [
        {"prompt_transport": "argv", "prompt_transport_arg": "-p"},
        {"prompt_transport_arg": "-"},
    ],
)
def test_pi_requires_stdin_without_prompt_argument(values) -> None:
    with pytest.raises(ValueError, match="stdin"):
        PI.validate_request(request(**values))


def test_pi_rejects_workflow_reasoning() -> None:
    with pytest.raises(ValueError, match="reasoning"):
        PI.validate_request(replace(request(), requested_reasoning="high"))


def test_pi_rejects_env_without_a_command() -> None:
    invocation = CliInvocationRequest(
        AgentConfig(cli_cmd=["env", "-i"], provider_kind="pi"), None
    )
    with pytest.raises(ValueError, match="executable"):
        PI.validate_request(invocation)


def test_pi_preserves_option_looking_native_values() -> None:
    PI.validate_request(request(extra_args=["--system-prompt", "--no-approve"]))


@pytest.mark.parametrize("text", ["", " \n\t"])
def test_pi_does_not_publish_stderr_as_an_answer(text) -> None:
    assert PI.output_extractor is not None
    output = PI.output_extractor(CommandResult(0, text, "diagnostic"), None)
    assert output.output_extraction_status == "missing"
    assert output.output_text == ""


def test_pi_strict_extractor_borrows_file_backed_stdout(tmp_path) -> None:
    stdout = tmp_path / "stdout"
    stdout.write_text("answer λ\n")
    assert PI.output_extractor is not None
    output = PI.output_extractor(CommandResult(0, "tail", "noise", stdout), None)
    assert output.output_extraction_status == "success"
    assert output.output_path == stdout
    assert output.output_char_count == len("answer λ\n")
    assert not output.owns_output_path


def test_pi_strict_extractor_retains_inline_stdout(tmp_path) -> None:
    assert PI.output_extractor is not None
    output = PI.output_extractor(
        CommandResult(0, "answer", "noise", tmp_path / "missing"), None
    )
    assert output.output_text == "answer"
    assert output.output_extraction_status == "success"


def test_pi_blank_capture_is_authoritative_over_inline_tail(tmp_path) -> None:
    stdout = tmp_path / "stdout"
    stdout.write_text(" \n")
    assert PI.output_extractor is not None
    output = PI.output_extractor(CommandResult(0, "stale", "noise", stdout), None)
    assert output.output_extraction_status == "missing"

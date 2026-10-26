import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from crewplane.adapters.invokers.cli import CliInvokerAdapter
from crewplane.adapters.invokers.cli_invoker import CliInvocationRequest
from crewplane.adapters.invokers.cli_invoker.command_strategy import command_strategy
from crewplane.architecture.contracts import CommandResult
from crewplane.core.config import AgentConfig, Config
from crewplane.version import SCHEMA_VERSION
from tests.helpers.cli_provider import command_receipts


def request_for(ollama, **config_fields):
    config = AgentConfig(
        cli_cmd=[str(ollama.executable)],
        provider_kind="ollama",
        invocation_timeout_seconds=60,
        **config_fields,
    )
    return CliInvocationRequest(
        config, "text-model", command_strategy=command_strategy()
    )


def test_exact_argv_utf8_and_custom_executable(ollama, tmp_path):
    request = replace(
        request_for(ollama, model_arg="--ignored"), working_directory=tmp_path
    )
    command = ollama.capability.build_command(request, "héllo 世界\n")
    assert command.cmd == [str(ollama.executable), "--model", "text-model"]
    assert command.stdin_data == "héllo 世界\n".encode()
    assert command.structured_output_file is None
    assert request.working_directory == tmp_path


@pytest.mark.parametrize("model", [None, "", " ", "--help", "bad\0model"])
def test_invalid_model(ollama, model):
    with pytest.raises(ValueError, match="model"):
        ollama.capability.validate_request(replace(request_for(ollama), model=model))


@pytest.mark.parametrize(
    "tokens",
    [
        ["other-model"],
        ["--nowordwrap"],
        ["--hidethinking=false"],
        ["--keepalive"],
        ["--format="],
        ["--format", "--verbose"],
        ["--verbose=no"],
        ["--think=invalid"],
        ["--think", "high"],
        ["--"],
        ["--unknown"],
        ["--model", "override"],
        ["--check"],
        ["--dimensions", "4"],
    ],
)
@pytest.mark.parametrize("location", ["cli_cmd", "extra_args"])
def test_competing_arguments_are_rejected_in_both_fields(ollama, tokens, location):
    request = request_for(ollama)
    value = [str(ollama.executable), *tokens] if location == "cli_cmd" else tokens
    request = replace(
        request, config=request.config.model_copy(update={location: value})
    )
    with pytest.raises(ValueError, match="Ollama"):
        ollama.capability.validate_request(request)


@pytest.mark.parametrize(
    "changes",
    [
        {"cli_cmd": ["ollama", "run"]},
        {"cli_cmd": ["ollama", "serve"]},
        {"prompt_transport": "argv", "prompt_transport_arg": "-p"},
        {"prompt_transport_arg": "-p"},
    ],
)
def test_unsupported_command_and_transport(ollama, changes):
    request = request_for(ollama)
    with pytest.raises(ValueError, match="Ollama"):
        ollama.capability.validate_request(
            replace(request, config=request.config.model_copy(update=changes))
        )


def test_reasoning_and_classification_contracts(ollama):
    with pytest.raises(ValueError, match="first-class reasoning"):
        ollama.capability.validate_request(
            replace(request_for(ollama), requested_reasoning="high")
        )
    for code in (0, 1):
        result = CommandResult(code, "quota exceeded", "quota exceeded")
        assert not ollama.capability.quota_classifier(
            result, ("quota",), datetime.now(UTC)
        ).is_quota
        assert ollama.capability.failure_classifier(result).kind == "provider_error"
    assert ollama.capability.usage_decoder is None
    assert ollama.capability.log_presentation_format == "plain"
    assert ollama.capability.supports_output_idle_timeout is False


@pytest.mark.parametrize(
    "scenario,success",
    [
        ("success", True),
        ("large", True),
        ("empty", False),
        ("stderr", False),
        ("partial", False),
        ("quota-success", True),
        ("quota-fail", False),
        ("retry", True),
    ],
)
def test_runtime_transport_output_and_retries(
    ollama, tmp_path, monkeypatch, scenario, success
):
    monkeypatch.setenv("CREWPLANE_FAKE_SCENARIO", scenario)
    request = request_for(
        ollama,
        max_retries=1,
        retry_delay_seconds=0,
        retry_on_exit_codes=[7],
        quota_reached_on_contains=["quota"],
    )
    config = Config(version=SCHEMA_VERSION, agents={"local": request.config})
    adapter = CliInvokerAdapter()
    options = adapter.canonicalize_options("cli", "cli", ollama.options).options
    invoker = adapter.create_invoker(config, options)
    output = tmp_path / "answer.txt"
    invocation = invoker.invoke(
        request.config,
        "override",
        "héllo 世界",
        output,
        tmp_path,
        log_file=tmp_path / "run.log",
    )
    if success:
        asyncio.run(invocation)
        assert output.read_text().strip()
        if scenario == "large":
            assert output.read_text().count("本") == 200_000
    else:
        with pytest.raises(RuntimeError):
            asyncio.run(invocation)
        assert not output.exists()
    receipts = command_receipts(ollama.receipts, "run")
    assert len(receipts) == (2 if scenario in {"retry", "quota-fail"} else 1)
    assert all(
        receipt["stdin"] == "héllo 世界" and receipt["cwd"] == str(tmp_path)
        for receipt in receipts
    )
    assert all(receipt["argv"][2] == "override" for receipt in receipts)
    assert not list(ollama.receipts.glob("check-*"))


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("-inf"), float("nan")])
def test_invalid_invocation_deadlines_are_rejected_without_probe(ollama, timeout):
    request = request_for(ollama)
    request = replace(
        request,
        config=request.config.model_copy(
            update={"invocation_timeout_seconds": timeout}
        ),
    )
    with pytest.raises(ValueError, match="finite positive invocation_timeout_seconds"):
        ollama.capability.validate_request(request)
    assert not list(ollama.receipts.iterdir())


@pytest.mark.parametrize(
    "timeout_fields",
    [{}, {"invocation_timeout_seconds": None}, {"invocation_timeout_seconds": 21600}],
)
def test_optional_or_multi_hour_invocation_deadline_is_valid(ollama, timeout_fields):
    request = replace(
        request_for(ollama),
        config=AgentConfig(
            cli_cmd=[str(ollama.executable)],
            provider_kind="ollama",
            **timeout_fields,
        ),
    )
    ollama.capability.validate_request(request)
    assert not list(ollama.receipts.iterdir())


def test_structural_validation_is_probe_free_and_accepts_custom_paths(ollama):
    request = request_for(ollama)
    request = replace(
        request,
        config=request.config.model_copy(
            update={"cli_cmd": ["/custom/not-installed-agent"]}
        ),
    )
    ollama.capability.validate_request(request)
    assert not list(ollama.receipts.iterdir())

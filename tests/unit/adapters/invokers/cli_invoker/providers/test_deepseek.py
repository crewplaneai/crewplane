from dataclasses import replace

import pytest

from crewplane.adapters.invokers.cli import collect_cli_request_errors
from crewplane.adapters.invokers.cli_invoker.capability import CliInvocationRequest
from crewplane.adapters.invokers.cli_invoker.providers.deepseek import DEEPSEEK
from crewplane.core.config import AgentConfig, Config
from crewplane.core.workflow.models import ProviderSpec, WorkflowNode, WorkflowPlan
from crewplane.version import SCHEMA_VERSION


def request(command=None, environment=None, **config_values) -> CliInvocationRequest:
    return CliInvocationRequest(
        AgentConfig(
            cli_cmd=command or ["dsh", "--profile", "headless"],
            provider_kind="deepseek",
            **(
                {"prompt_transport": "argv", "prompt_transport_arg": "--"}
                | config_values
            ),
        ),
        None,
        environment={"DSH_PERMISSION_MODE": "danger-full-access"}
        if environment is None
        else environment,
    )


@pytest.mark.parametrize(
    "launcher, extra_args",
    [
        (["--profile", "headless"], []),
        (
            ["--profile=headless"],
            ["--patch", "native.yml", "--from-default-profile", "headless"],
        ),
        (
            [
                "--patch=native.yml",
                "--profile=headless",
                "--from-default-profile",
                "headless",
            ],
            [],
        ),
    ],
)
def test_deepseek_appends_exactly_two_separators_after_launcher_arguments(
    monkeypatch, launcher, extra_args
) -> None:
    monkeypatch.setenv("PATH", "")
    invocation = request(
        ["env", "DSH_PERMISSION_MODE=danger-full-access", "dsh", *launcher],
        extra_args=extra_args,
    )
    DEEPSEEK.validate_request(invocation)
    command = DEEPSEEK.build_command(invocation, '--help\n"λ"')
    assert command.cmd == [
        *invocation.config.cli_cmd,
        *invocation.config.extra_args,
        "--",
        "--",
        '--help\n"λ"',
    ]
    assert command.stdin_data is None
    assert command.structured_output_file is None
    assert DEEPSEEK.supports_output_idle_timeout
    assert DEEPSEEK.usage_decoder is None
    assert DEEPSEEK.log_presentation_format == "plain"


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--profile", "tui"],
        ["--profile="],
        ["--profile"],
        ["--profile", "headless", "--profile", "web"],
        ["--profile", "headless", "--dump-config"],
        ["--profile", "headless", "--dump-default-config"],
        ["--profile", "headless", "--from-default-profile", "tui"],
        ["--profile", "headless", "--patch"],
        ["--profile", "headless", "--patch="],
        ["--profile", "headless", "--"],
        ["web"],
        ["plugin"],
        ["--help"],
        ["-h"],
        ["--version"],
        ["-V"],
        ["--profile", "headless", "--resume", "saved"],
        ["--profile", "headless", "--model", "native"],
        ["--profile", "headless", "configured task"],
    ],
)
@pytest.mark.parametrize("location", ["cli_cmd", "extra_args"])
def test_deepseek_rejects_missing_or_conflicting_launcher_selectors(
    arguments, location
) -> None:
    invocation = (
        request(["dsh", *arguments])
        if location == "cli_cmd"
        else request(["dsh"], extra_args=arguments)
    )
    with pytest.raises(ValueError, match="DeepSeek"):
        DEEPSEEK.validate_request(invocation)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--api-key=credential-sentinel"],
        ["--api-key", "credential-sentinel"],
        ["credential-sentinel"],
        ["--credential-sentinel"],
    ],
)
@pytest.mark.parametrize("location", ["cli_cmd", "extra_args"])
def test_deepseek_rejected_arguments_do_not_disclose_values(
    arguments, location
) -> None:
    invocation = request()
    getattr(invocation.config, location).extend(arguments)

    with pytest.raises(ValueError) as caught:
        DEEPSEEK.validate_request(invocation)

    assert "credential-sentinel" not in str(caught.value)
    assert "launcher argument at position 3" in str(caught.value)


@pytest.mark.parametrize(
    "arguments, position",
    [
        (["--api-key=credential-sentinel"], 2),
        (["--api-key", "credential-sentinel"], 2),
        (["--credential-sentinel"], 2),
        (["--ignore-environment=credential-sentinel"], 2),
        (["-v", "-xcredential-sentinel"], 3),
    ],
)
def test_deepseek_rejected_wrapper_arguments_do_not_disclose_values(
    arguments: list[str], position: int
) -> None:
    invocation = request(
        [
            "env",
            *arguments,
            "DSH_PERMISSION_MODE=danger-full-access",
            "dsh",
            "--profile",
            "headless",
        ]
    )

    with pytest.raises(ValueError) as caught:
        DEEPSEEK.validate_request(invocation)

    assert "credential-sentinel" not in str(caught.value)
    assert str(caught.value) == (
        f"Cannot validate env wrapper argument at cli_cmd position {position}."
    )


@pytest.mark.parametrize(
    "prefix, inherited, accepted",
    [
        ([], None, False),
        ([], "workspace-write", False),
        ([], "danger-full-access", True),
        (["env", "-i"], "danger-full-access", False),
        (["env", "-u", "DSH_PERMISSION_MODE"], "danger-full-access", False),
        (["env", "--unset=DSH_PERMISSION_MODE"], "danger-full-access", False),
        (["env", "DSH_PERMISSION_MODE="], "danger-full-access", False),
        (["env", "DSH_PERMISSION_MODE=workspace-write"], "danger-full-access", False),
        (["/usr/bin/env", "-i", "DSH_PERMISSION_MODE=danger-full-access"], None, True),
        (
            [
                "env",
                "-u",
                "DSH_PERMISSION_MODE",
                "DSH_PERMISSION_MODE=danger-full-access",
            ],
            "workspace-write",
            True,
        ),
        (["env", "--", "DSH_PERMISSION_MODE=danger-full-access"], None, True),
        (
            ["env", "-C", "workspace", "DSH_PERMISSION_MODE=danger-full-access"],
            None,
            True,
        ),
    ],
)
def test_deepseek_checks_effective_permission_environment(
    prefix, inherited, accepted
) -> None:
    environment = {} if inherited is None else {"DSH_PERMISSION_MODE": inherited}
    invocation = request([*prefix, "dsh", "--profile", "headless"], environment)
    if accepted:
        DEEPSEEK.validate_request(invocation)
    else:
        with pytest.raises(ValueError, match="DSH_PERMISSION_MODE=danger-full-access"):
            DEEPSEEK.validate_request(invocation)


@pytest.mark.parametrize(
    "values",
    [
        {"prompt_transport": "stdin", "prompt_transport_arg": None},
        {"prompt_transport_arg": "--prompt"},
    ],
)
def test_deepseek_requires_explicit_argv_transport(values) -> None:
    with pytest.raises(ValueError, match="argv"):
        DEEPSEEK.validate_request(request(**values))


def test_deepseek_rejects_resolved_model_and_reasoning() -> None:
    with pytest.raises(ValueError, match="native model"):
        DEEPSEEK.validate_request(replace(request(), model="requested"))
    with pytest.raises(ValueError, match="reasoning"):
        DEEPSEEK.validate_request(replace(request(), requested_reasoning="high"))


def test_deepseek_rejects_env_without_a_command() -> None:
    with pytest.raises(ValueError, match="executable"):
        DEEPSEEK.validate_request(
            request(["env", "DSH_PERMISSION_MODE=danger-full-access"])
        )


def test_deepseek_validation_does_not_inspect_native_settings(
    tmp_path, monkeypatch
) -> None:
    settings = tmp_path / "settings.yaml"
    settings.write_text("permission:\n  defaultPreset: workspace-write\n")
    invocation = request(
        environment={
            "DSH_HOME": str(tmp_path),
            "DSH_PERMISSION_MODE": "danger-full-access",
        }
    )

    def unexpected_read(*args, **kwargs):
        raise AssertionError(f"validator inspected native settings: {args}, {kwargs}")

    monkeypatch.setattr(type(settings), "read_text", unexpected_read)
    DEEPSEEK.validate_request(invocation)
    assert settings.read_bytes() == b"permission:\n  defaultPreset: workspace-write\n"


@pytest.mark.parametrize(
    "default_model, workflow_model",
    [("default", None), (None, "override"), ("default", "override")],
)
def test_deepseek_preflight_rejects_models_from_both_sources(
    default_model, workflow_model
) -> None:
    agent = request(default_model=default_model).config
    workflow = WorkflowPlan(
        name="test",
        nodes=[
            WorkflowNode(
                id="node",
                mode="sequential",
                providers=[ProviderSpec(provider="deepseek", model=workflow_model)],
            )
        ],
    )
    errors = collect_cli_request_errors(
        workflow,
        Config(version=SCHEMA_VERSION, agents={"deepseek": agent}),
        environment={"DSH_PERMISSION_MODE": "danger-full-access"},
    )
    assert len(errors) == 1
    assert "native model" in errors[0]

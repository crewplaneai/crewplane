from dataclasses import replace
from pathlib import Path

import pytest

from crewplane.adapters.invokers.cli import collect_cli_request_errors
from crewplane.adapters.invokers.cli_invoker.capabilities import (
    build_cli_invocation_plan,
)
from crewplane.adapters.invokers.cli_invoker.capability import CliInvocationRequest
from crewplane.adapters.invokers.cli_invoker.failures.classifier import (
    classify_generic_failure,
)
from crewplane.adapters.invokers.cli_invoker.providers.opencode import OPENCODE
from crewplane.adapters.invokers.cli_invoker.quota.classifier import (
    classify_generic_quota,
)
from crewplane.core.config import AgentConfig, Config
from crewplane.core.preflight.execution_nodes import resolve_provider_model
from crewplane.core.workflow.models import ProviderSpec, WorkflowNode, WorkflowPlan
from crewplane.version import SCHEMA_VERSION


def request(command=None, model=None, **values) -> CliInvocationRequest:
    return CliInvocationRequest(
        AgentConfig(
            cli_cmd=command or ["opencode", "run"], provider_kind="opencode", **values
        ),
        model,
    )


@pytest.mark.parametrize("model", [None, "provider/model/unchanged"])
def test_managed_command_preserves_literal_stdin_and_native_policy(monkeypatch, model):
    monkeypatch.setenv("PATH", "")
    invocation = request(model=model, extra_args=["--variant", "high"])
    OPENCODE.validate_request(invocation)
    prompt = "--help\n\"quoted\" 'literal' λ 中文\n"
    command = OPENCODE.build_command(invocation, prompt)
    assert command.cmd == [
        "opencode",
        "run",
        *(["--model", model] if model else []),
        "--variant",
        "high",
        "--format",
        "json",
        "--dir",
        str(Path.cwd()),
    ]
    assert command.stdin_data == prompt.encode("utf-8")
    assert prompt not in command.cmd
    assert command.structured_output_file is None


def test_capability_uses_shared_classifiers_and_lifecycle():
    assert OPENCODE.quota_classifier is classify_generic_quota
    assert OPENCODE.failure_classifier is classify_generic_failure
    assert OPENCODE.supports_output_idle_timeout
    assert OPENCODE.one_shot_failure_retry is None


def test_raw_model_is_preserved_when_no_crewplane_model_resolves(monkeypatch):
    monkeypatch.setenv("PATH", "")
    invocation = request(extra_args=["-m=provider/model", "--agent=first"])
    OPENCODE.validate_request(invocation)
    command = OPENCODE.build_command(invocation, "prompt")
    assert command.cmd == [
        "opencode",
        "run",
        *invocation.config.extra_args,
        "--format",
        "json",
        "--dir",
        str(Path.cwd()),
    ]


@pytest.mark.parametrize("directory_kind", ["absolute", "relative", "omitted"])
@pytest.mark.parametrize("prefix", [[], ["env", "PWD=/inherited/checkout"]])
def test_managed_directory_is_absolute_and_independent_of_pwd(
    tmp_path, monkeypatch, directory_kind, prefix
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", "/inherited/checkout")
    workspace = tmp_path / "workspace λ"
    workspace.mkdir()
    directories = {
        "absolute": workspace,
        "relative": Path(workspace.name),
        "omitted": None,
    }
    invocation = replace(
        request([*prefix, "opencode", "run"]),
        working_directory=directories[directory_kind],
    )
    OPENCODE.validate_request(invocation)
    command = OPENCODE.build_command(invocation, "prompt")
    expected = tmp_path if directory_kind == "omitted" else workspace
    assert command.cmd[-4:] == ["--format", "json", "--dir", str(expected)]
    assert command.cmd.count("--dir") == 1


@pytest.mark.parametrize(
    "wrapper_args", [["--split-string", "secret"], ["--unknown-secret"], ["--unset"]]
)
def test_unsupported_env_syntax_has_redacted_opencode_diagnostic(wrapper_args):
    with pytest.raises(ValueError, match="OpenCode.*env wrapper") as caught:
        OPENCODE.validate_request(request(["env", *wrapper_args]))
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "option", ["--model", "-m", "--agent", "--variant", "--title", "--log-level"]
)
@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("location", ["cli_cmd", "extra_args"])
def test_value_options_accept_split_and_equals_forms(option, inline, location):
    invocation = request()
    getattr(invocation.config, location).extend(
        [f"{option}=native value"] if inline else [option, "native value"]
    )
    OPENCODE.validate_request(invocation)


@pytest.mark.parametrize("option", ["--agent", "--variant", "--title", "--log-level"])
@pytest.mark.parametrize(
    "locations",
    [("cli_cmd", "cli_cmd"), ("extra_args", "extra_args"), ("cli_cmd", "extra_args")],
)
@pytest.mark.parametrize("second_value", ["first-secret", "second-secret"])
def test_repeated_value_options_are_rejected_across_argument_lists(
    option, locations, second_value
):
    invocation = request()
    getattr(invocation.config, locations[0]).extend([option, "first-secret"])
    getattr(invocation.config, locations[1]).append(f"{option}={second_value}")

    with pytest.raises(ValueError) as caught:
        OPENCODE.validate_request(invocation)

    assert str(caught.value) == (
        f"OpenCode option {option!r} may appear at most once across cli_cmd and extra_args."
    )
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("flag", ["--thinking", "--auto", "--print-logs"])
@pytest.mark.parametrize("suffix", ["", "=true", "=false"])
def test_boolean_options_accept_only_explicit_native_forms(flag, suffix):
    OPENCODE.validate_request(request(extra_args=[flag + suffix]))


@pytest.mark.parametrize(
    "arguments",
    [
        ["--format", "json"],
        ["--format=json"],
        ["--continue"],
        ["-c"],
        ["--session", "secret"],
        ["--fork"],
        ["--attach=secret"],
        ["--port", "9000"],
        ["--password=secret"],
        ["--username=secret"],
        ["--dir=secret"],
        ["--dir", "secret"],
        ["--file=secret"],
        ["--command=secret"],
        ["--interactive"],
        ["--mini"],
        ["--share"],
        ["--help"],
        ["--version"],
        ["--yolo"],
        ["--"],
        ["positional-secret"],
        ["auth"],
        ["-msecret"],
        ["-mv"],
        ["--unknown-secret"],
        ["--thinking=yes-secret"],
        ["--auto", "false"],
        ["--no-thinking"],
    ],
)
@pytest.mark.parametrize("location", ["cli_cmd", "extra_args"])
def test_arguments_outside_managed_grammar_are_rejected_without_values(
    arguments, location
):
    invocation = request()
    getattr(invocation.config, location).extend(arguments)
    with pytest.raises(ValueError, match="OpenCode") as caught:
        OPENCODE.validate_request(invocation)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "option", ["--model", "-m", "--agent", "--variant", "--title", "--log-level"]
)
@pytest.mark.parametrize("ending", [[], ["--"], ["--auto"]])
@pytest.mark.parametrize("location", ["cli_cmd", "extra_args"])
def test_missing_values_cannot_consume_other_sequences_or_managed_flags(
    option, ending, location
):
    invocation = request()
    getattr(invocation.config, location).extend([option, *ending])
    with pytest.raises(ValueError, match="requires a nonblank value"):
        OPENCODE.validate_request(invocation)


@pytest.mark.parametrize("value", ["", "  ", "--", "--auto"])
def test_inline_option_values_must_be_nonblank_values(value):
    with pytest.raises(ValueError, match="--agent"):
        OPENCODE.validate_request(request(extra_args=[f"--agent={value}"]))


def test_command_and_extra_arguments_are_validated_separately():
    with pytest.raises(ValueError, match="--title"):
        OPENCODE.validate_request(
            request(["opencode", "run", "--title"], extra_args=["title"])
        )


@pytest.mark.parametrize(
    "command,extra_args,model",
    [
        (["opencode", "run", "-m", "raw"], [], "resolved"),
        (["opencode", "run"], ["--model=raw"], "resolved"),
        (["opencode", "run", "-m=first"], ["--model", "second"], None),
        (["opencode", "run"], ["-m=first", "-m=first"], None),
    ],
)
def test_model_selectors_cannot_conflict(command, extra_args, model):
    with pytest.raises(ValueError, match="model"):
        OPENCODE.validate_request(request(command, model, extra_args=extra_args))


@pytest.mark.parametrize("source", ["workflow", "default"])
@pytest.mark.parametrize(
    "model", ["--continue", "--auto", "--session=ses_private", "-c", "--", " \t"]
)
def test_invalid_resolved_models_fail_before_command_construction(
    tmp_path, monkeypatch, source, model
):
    config = request(default_model=model if source == "default" else None).config
    provider = ProviderSpec(
        provider="opencode", model=model if source == "workflow" else None
    )

    def unexpected(*args, **kwargs):
        del args, kwargs
        raise AssertionError("invalid models must fail before command construction")

    monkeypatch.setattr(
        "crewplane.adapters.invokers.cli_invoker.providers.opencode.build_standard_command",
        unexpected,
    )
    with pytest.raises(ValueError) as caught:
        build_cli_invocation_plan(
            config,
            resolve_provider_model(provider, config),
            "prompt",
            tmp_path / "answer.md",
        )
    assert str(caught.value) == "OpenCode option '--model' requires a nonblank value."
    assert list(tmp_path.iterdir()) == []


def test_valid_workflow_model_overrides_invalid_unused_default(tmp_path):
    config = request(default_model="--continue").config
    provider = ProviderSpec(provider="opencode", model="vendor/workflow/model")
    plan = build_cli_invocation_plan(
        config,
        resolve_provider_model(provider, config),
        "prompt",
        tmp_path / "answer.md",
    )
    assert plan.cmd[2:4] == ["--model", "vendor/workflow/model"]
    assert "--continue" not in plan.cmd


@pytest.mark.parametrize(
    "prefix",
    [
        [],
        ["env"],
        ["env", "-i", "SETTING=literal"],
        ["env", "--", "SETTING=value"],
        ["env", "-u", "SETTING"],
        ["env", "--unset=SETTING"],
        ["env", "-v", "-P", "/usr/bin"],
    ],
)
def test_existing_env_forms_and_absolute_executable_are_supported(tmp_path, prefix):
    executable = tmp_path / "renamed-provider"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    invocation = request([*prefix, str(executable), "run"])
    OPENCODE.validate_request(invocation)
    assert str(executable) in OPENCODE.build_command(invocation, "prompt").cmd


@pytest.mark.parametrize("option", [["-C", "secret"], ["-Csecret"], ["--chdir=secret"]])
def test_wrapper_cannot_change_runtime_working_directory(option):
    with pytest.raises(ValueError, match="working directory") as caught:
        OPENCODE.validate_request(request(["env", *option, "opencode", "run"]))
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "command",
    [
        ["opencode"],
        ["opencode", "auth"],
        ["opencode", "--print-logs", "run"],
        ["env", "SETTING=value"],
    ],
)
def test_run_must_immediately_follow_executable(command):
    with pytest.raises(ValueError, match="OpenCode.*(run|executable)"):
        OPENCODE.validate_request(request(command))


@pytest.mark.parametrize(
    "values",
    [
        {"prompt_transport_arg": "--prompt"},
        {"prompt_transport": "argv", "prompt_transport_arg": "--"},
    ],
)
def test_stdin_without_prompt_argument_is_required(values):
    with pytest.raises(ValueError, match="stdin"):
        OPENCODE.validate_request(request(**values))


def test_reasoning_is_rejected_and_validation_allocates_nothing(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        del args, kwargs
        raise AssertionError("validation must not launch or allocate")

    monkeypatch.setattr("tempfile.mkstemp", unexpected)
    monkeypatch.setattr("subprocess.Popen", unexpected)
    invocation = replace(request(), requested_reasoning="high")
    with pytest.raises(ValueError, match="reasoning"):
        OPENCODE.validate_request(invocation)
    with pytest.raises(ValueError, match="--format"):
        build_cli_invocation_plan(
            request(extra_args=["--format=json"]).config,
            None,
            "prompt",
            tmp_path / "out.md",
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "default,workflow_model,conflict",
    [
        (None, None, False),
        ("default", None, True),
        (None, "override", True),
        ("default", "override", True),
    ],
)
def test_preflight_uses_existing_model_resolution_with_arbitrary_alias(
    default, workflow_model, conflict
):
    config = Config(
        version=SCHEMA_VERSION,
        agents={
            "my-agent": request(default_model=default, extra_args=["-m=raw"]).config
        },
    )
    workflow = WorkflowPlan(
        name="test",
        nodes=[
            WorkflowNode(
                id="node",
                mode="sequential",
                providers=[ProviderSpec(provider="my-agent", model=workflow_model)],
            )
        ],
    )
    errors = collect_cli_request_errors(workflow, config, working_directory=Path.cwd())
    assert bool(errors) is conflict
    if conflict:
        assert "model" in errors[0]

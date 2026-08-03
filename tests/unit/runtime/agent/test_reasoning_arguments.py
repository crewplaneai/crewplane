from pathlib import Path

import pytest

from crewplane.adapters.invokers.cli_invoker.reasoning import (
    CLAUDE_REASONING_ENV,
    build_reasoning_args,
    validate_reasoning_request,
)
from crewplane.core.config import AgentConfig


@pytest.fixture(autouse=True)
def clear_ambient_claude_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CLAUDE_REASONING_ENV, raising=False)


def config(
    provider_kind: str,
    *cli_cmd: str,
    extra_args: list[str] | None = None,
) -> AgentConfig:
    return AgentConfig(
        cli_cmd=list(cli_cmd) or [provider_kind],
        provider_kind=provider_kind,
        extra_args=[] if extra_args is None else extra_args,
    )


def test_reasoning_args_are_native_and_none_is_a_noop() -> None:
    codex = config("codex", "codex")
    claude = config("claude", "claude")

    assert build_reasoning_args(codex, None) == ()
    assert build_reasoning_args(codex, "high") == (
        "--config",
        'model_reasoning_effort="high"',
    )
    assert build_reasoning_args(claude, "high") == ("--effort", "high")


def test_reasoning_request_rejects_unsupported_provider() -> None:
    with pytest.raises(ValueError, match="provider_kind 'codex' or 'claude'"):
        validate_reasoning_request(config("generic", "provider"), "high")


@pytest.mark.parametrize(
    ("tokens", "message"),
    [
        pytest.param(
            ["codex", "--"],
            "cli_cmd option terminator",
            id="command-terminator",
        ),
        pytest.param(
            ["codex", "--config"],
            "--config requires a TOML assignment",
            id="config-missing-value",
        ),
        pytest.param(
            ["codex", "-c", "--"],
            "cli_cmd option terminator",
            id="short-config-terminator",
        ),
        pytest.param(
            ["codex", "--config=missing_equals"],
            "--config requires a TOML key=value assignment",
            id="long-config-malformed",
        ),
        pytest.param(
            ["codex", "-cmissing_equals"],
            "-c requires a TOML key=value assignment",
            id="attached-config-malformed",
        ),
        pytest.param(
            ["codex", "-c=model_reasoning_effort=low"],
            "model_reasoning_effort conflicts",
            id="short-config-conflict",
        ),
    ],
)
def test_codex_reasoning_rejects_ambiguous_or_conflicting_command_tokens(
    tokens: list[str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_reasoning_request(config("codex", *tokens), "high")


@pytest.mark.parametrize(
    ("tokens", "message"),
    [
        pytest.param(
            ["claude", "--effort"],
            "--effort requires a value",
            id="effort-missing",
        ),
        pytest.param(
            ["claude", "--effort=low"],
            "--effort conflicts",
            id="effort-inline",
        ),
        pytest.param(
            ["claude", "--settings"],
            "--settings requires a JSON object or file path",
            id="settings-missing",
        ),
        pytest.param(
            ["claude", "--settings="],
            "--settings requires a JSON object or file path",
            id="settings-empty-inline",
        ),
        pytest.param(
            ["claude", "--settings={bad"],
            "valid JSON object",
            id="settings-invalid-json",
        ),
        pytest.param(
            ["claude", '--settings={"effortLevel":true}'],
            "effortLevel conflicts",
            id="settings-non-string-effort",
        ),
        pytest.param(
            [
                "claude",
                '--settings={"env":{"CLAUDE_CODE_EFFORT_LEVEL":true}}',
            ],
            "CLAUDE_CODE_EFFORT_LEVEL conflicts",
            id="settings-non-string-environment",
        ),
    ],
)
def test_claude_reasoning_rejects_ambiguous_or_conflicting_tokens(
    tokens: list[str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_reasoning_request(config("claude", *tokens), "high", environment={})


def test_claude_reasoning_accepts_blank_and_non_mapping_settings_environment() -> None:
    for settings in (
        '{"effortLevel":" ","env":"not-a-mapping"}',
        '{"env":{"CLAUDE_CODE_EFFORT_LEVEL":" "}}',
    ):
        validate_reasoning_request(
            config("claude", "claude", f"--settings={settings}"),
            "high",
            environment={},
        )


def test_claude_reasoning_resolves_relative_settings_file(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("{}", encoding="utf-8")

    validate_reasoning_request(
        config("claude", "claude", "--settings", "settings.json"),
        "high",
        environment={},
        working_directory=tmp_path,
    )


def test_claude_reasoning_rejects_non_object_settings_file(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="must contain a JSON object"):
        validate_reasoning_request(
            config("claude", "claude", "--settings", "settings.json"),
            "high",
            environment={},
            working_directory=tmp_path,
        )


@pytest.mark.parametrize(
    ("cli_cmd", "clears_inherited"),
    [
        pytest.param(
            ["env", "--ignore-environment", "claude"],
            True,
            id="ignore-environment",
        ),
        pytest.param(
            ["env", "--unset=CLAUDE_CODE_EFFORT_LEVEL", "claude"],
            True,
            id="unset-inline",
        ),
        pytest.param(
            ["env", "-uCLAUDE_CODE_EFFORT_LEVEL", "claude"],
            True,
            id="unset-short-attached",
        ),
        pytest.param(
            ["env", "-iv", "claude"],
            True,
            id="short-flags",
        ),
        pytest.param(
            ["env", "--debug", "--argv0=claude", "claude"],
            False,
            id="supported-long-options",
        ),
        pytest.param(
            ["env", "-Pbin", "claude"],
            False,
            id="supported-short-value-option",
        ),
        pytest.param(
            ["env", "-", "claude"],
            True,
            id="dash-clears-environment",
        ),
        pytest.param(
            ["env", "--", "claude"],
            False,
            id="option-terminator",
        ),
    ],
)
def test_env_prefix_can_remove_inherited_reasoning_or_use_safe_options(
    cli_cmd: list[str],
    clears_inherited: bool,
) -> None:
    validate_reasoning_request(
        config("claude", *cli_cmd),
        "high",
        environment=({CLAUDE_REASONING_ENV: "max"} if clears_inherited else {}),
    )


@pytest.mark.parametrize(
    ("cli_cmd", "message"),
    [
        pytest.param(
            ["env", "--unknown", "claude"],
            "Cannot validate env option '--unknown'",
            id="unknown-long",
        ),
        pytest.param(
            ["env", "--unset"],
            "--unset requires a value",
            id="unset-missing-value",
        ),
        pytest.param(
            ["env", "-x", "claude"],
            "Cannot validate env option '-x'",
            id="unknown-short",
        ),
        pytest.param(
            ["env", "-Ctmp", "claude"],
            "-C cannot be combined",
            id="chdir-short",
        ),
        pytest.param(
            ["env", "--chdir=tmp", "claude"],
            "--chdir cannot be combined",
            id="chdir-long",
        ),
        pytest.param(
            ["env", "--split-string=x", "claude"],
            "--split-string cannot be combined",
            id="split-string",
        ),
    ],
)
def test_env_prefix_rejects_options_that_cannot_be_validated(
    cli_cmd: list[str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_reasoning_request(
            config("claude", *cli_cmd),
            "high",
            environment={},
        )

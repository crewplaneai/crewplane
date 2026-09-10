import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.adapters.invokers.cli_invoker import (
    build_cli_invocation_plan,
)
from crewplane.architecture.contracts import (
    InvocationContext,
)
from crewplane.core.config import AgentConfig


def test_codex_reasoning_request_builds_native_config_before_extra_args() -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="codex",
        role="executor",
        requested_reasoning="xhigh",
    )
    plan = build_cli_invocation_plan(
        AgentConfig(
            cli_cmd=["codex", "exec"],
            provider_kind="codex",
            extra_args=["--ephemeral"],
        ),
        model="gpt-6-astra",
        prompt="prompt",
        output_file=Path("output.md"),
        invocation_context=context,
    )

    assert plan.cmd[1:8] == [
        "exec",
        "--model",
        "gpt-6-astra",
        "--config",
        'model_reasoning_effort="xhigh"',
        "--ephemeral",
        "--json",
    ]
    assert "requested_reasoning: xhigh\n" in plan.log_header.decode()


def test_claude_reasoning_request_builds_native_effort() -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="claude",
        role="reviewer",
        requested_reasoning="high",
    )
    with patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}):
        plan = build_cli_invocation_plan(
            AgentConfig(cli_cmd=["claude"], provider_kind="claude"),
            model=None,
            prompt="prompt",
            output_file=Path("output.md"),
            invocation_context=context,
        )

    assert plan.cmd[1:3] == ["--effort", "high"]


def test_reasoning_request_rejects_claude_settings_effort(
    subtests: pytest.Subtests,
) -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="claude",
        role="executor",
        requested_reasoning="high",
    )
    configs = (
        AgentConfig(
            cli_cmd=[
                "claude",
                "--settings",
                '{"effortLevel": "low"}',
            ],
            provider_kind="claude",
        ),
        AgentConfig(
            cli_cmd=["claude"],
            extra_args=['--settings={"effortLevel": "low"}'],
            provider_kind="claude",
        ),
    )

    with patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}):
        for config in configs:
            with (
                subtests.test(config=config),
                pytest.raises(ValueError, match="--settings effortLevel"),
            ):
                build_cli_invocation_plan(
                    config,
                    model=None,
                    prompt="prompt",
                    output_file=Path("output.md"),
                    invocation_context=context,
                )


def test_reasoning_request_rejects_claude_settings_environment() -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="claude",
        role="executor",
        requested_reasoning="high",
    )
    config = AgentConfig(
        cli_cmd=[
            "claude",
            "--settings",
            '{"env": {"CLAUDE_CODE_EFFORT_LEVEL": "low"}}',
        ],
        provider_kind="claude",
    )

    with (
        patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}),
        pytest.raises(ValueError, match="CLAUDE_CODE_EFFORT_LEVEL"),
    ):
        build_cli_invocation_plan(
            config,
            model=None,
            prompt="prompt",
            output_file=Path("output.md"),
            invocation_context=context,
        )


def test_reasoning_request_allows_unrelated_claude_settings() -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="claude",
        role="executor",
        requested_reasoning="high",
    )
    config = AgentConfig(
        cli_cmd=[
            "claude",
            "--settings",
            (
                '{"permissions": {"allow": ["Read"]}, '
                '"effortLevel": "", '
                '"env": {"CLAUDE_CODE_EFFORT_LEVEL": ""}}'
            ),
        ],
        provider_kind="claude",
    )

    with patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}):
        plan = build_cli_invocation_plan(
            config,
            model=None,
            prompt="prompt",
            output_file=Path("output.md"),
            invocation_context=context,
        )

    assert "--effort" in plan.cmd


def test_reasoning_request_checks_relative_claude_settings_file() -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="claude",
        role="executor",
        requested_reasoning="high",
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        working_directory = Path(tmp_dir)
        (working_directory / "claude-settings.json").write_text(
            '{"effortLevel": "low"}',
            encoding="utf-8",
        )
        config = AgentConfig(
            cli_cmd=["claude", "--settings", "claude-settings.json"],
            provider_kind="claude",
        )

        with (
            patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}),
            pytest.raises(ValueError, match="--settings effortLevel"),
        ):
            build_cli_invocation_plan(
                config,
                model=None,
                prompt="prompt",
                output_file=working_directory / "output.md",
                invocation_context=context,
                working_directory=working_directory,
            )


def test_reasoning_request_fails_closed_for_unreadable_claude_settings() -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="claude",
        role="executor",
        requested_reasoning="high",
    )
    config = AgentConfig(
        cli_cmd=["claude", "--settings", "missing-settings.json"],
        provider_kind="claude",
    )

    with (
        patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}),
        pytest.raises(ValueError, match="Cannot validate"),
    ):
        build_cli_invocation_plan(
            config,
            model=None,
            prompt="prompt",
            output_file=Path("output.md"),
            invocation_context=context,
        )


def test_reasoning_request_rejects_direct_codex_config_conflict(
    subtests: pytest.Subtests,
) -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="codex",
        role="executor",
        requested_reasoning="high",
    )
    conflict_forms = (
        ["--config", 'model_reasoning_effort="low"'],
        ['--config=model_reasoning_effort="low"'],
        ["-c", 'model_reasoning_effort = "low"'],
        ['-c=model_reasoning_effort="low"'],
        ['-cmodel_reasoning_effort="low"'],
    )

    for extra_args in conflict_forms:
        with (
            subtests.test(extra_args=extra_args),
            pytest.raises(ValueError, match="model_reasoning_effort"),
        ):
            build_cli_invocation_plan(
                AgentConfig(
                    cli_cmd=["codex", "exec"],
                    provider_kind="codex",
                    extra_args=extra_args,
                ),
                model="gpt-5.5",
                prompt="prompt",
                output_file=Path("output.md"),
                invocation_context=context,
            )


def test_codex_reasoning_conflict_scan_respects_extra_args_terminator() -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="codex",
        role="executor",
        requested_reasoning="high",
    )

    plan = build_cli_invocation_plan(
        AgentConfig(
            cli_cmd=["codex", "exec"],
            provider_kind="codex",
            extra_args=["--", 'model_reasoning_effort="low"'],
        ),
        model=None,
        prompt="prompt",
        output_file=Path("output.md"),
        invocation_context=context,
    )

    assert plan.cmd[2:6] == [
        "--config",
        'model_reasoning_effort="high"',
        "--",
        'model_reasoning_effort="low"',
    ]


def test_codex_reasoning_allows_env_prefix_provider_like_tokens() -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="codex",
        role="executor",
        requested_reasoning="high",
    )

    plan = build_cli_invocation_plan(
        AgentConfig(
            cli_cmd=["env", "-u", "--config", "codex"],
            provider_kind="codex",
        ),
        model=None,
        prompt="prompt",
        output_file=Path("output.md"),
        invocation_context=context,
    )

    command_index = plan.cmd.index("codex")
    assert plan.cmd[command_index + 1 : command_index + 3] == [
        "--config",
        'model_reasoning_effort="high"',
    ]


def test_reasoning_request_rejects_cli_command_option_terminator() -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="codex",
        role="executor",
        requested_reasoning="high",
    )

    with pytest.raises(ValueError, match="option terminator"):
        build_cli_invocation_plan(
            AgentConfig(
                cli_cmd=["codex", "exec", "--"],
                provider_kind="codex",
            ),
            model=None,
            prompt="prompt",
            output_file=Path("output.md"),
            invocation_context=context,
        )


def test_reasoning_request_rejects_unsupported_provider_kind() -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="generic",
        role="executor",
        requested_reasoning="high",
    )

    with pytest.raises(ValueError, match="provider_kind 'codex' or 'claude'"):
        build_cli_invocation_plan(
            AgentConfig(cli_cmd=["provider"], provider_kind="generic"),
            model=None,
            prompt="prompt",
            output_file=Path("output.md"),
            invocation_context=context,
        )

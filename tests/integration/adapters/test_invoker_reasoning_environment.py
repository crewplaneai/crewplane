import os
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


def test_reasoning_request_rejects_claude_environment_conflict() -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="claude",
        role="executor",
        requested_reasoning="high",
    )
    with (
        patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": "low"}),
        pytest.raises(ValueError, match="CLAUDE_CODE_EFFORT_LEVEL"),
    ):
        build_cli_invocation_plan(
            AgentConfig(cli_cmd=["claude"], provider_kind="claude"),
            model=None,
            prompt="prompt",
            output_file=Path("output.md"),
            invocation_context=context,
        )


def test_reasoning_request_rejects_claude_cli_environment_assignment() -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="claude",
        role="executor",
        requested_reasoning="high",
    )
    config = AgentConfig(
        cli_cmd=["env", "CLAUDE_CODE_EFFORT_LEVEL=low", "claude"],
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


def test_reasoning_request_allows_blank_claude_cli_environment_assignment() -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="claude",
        role="executor",
        requested_reasoning="high",
    )
    config = AgentConfig(
        cli_cmd=["env", "CLAUDE_CODE_EFFORT_LEVEL=", "claude"],
        provider_kind="claude",
    )

    with patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": "low"}):
        plan = build_cli_invocation_plan(
            config,
            model=None,
            prompt="prompt",
            output_file=Path("output.md"),
            invocation_context=context,
        )

    assert "--effort" in plan.cmd


def test_reasoning_request_rejects_path_qualified_env_assignment() -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="claude",
        role="executor",
        requested_reasoning="high",
    )
    config = AgentConfig(
        cli_cmd=[
            "/usr/bin/env",
            "-i",
            "CLAUDE_CODE_EFFORT_LEVEL=low",
            "claude",
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


def test_reasoning_request_allows_assignment_tokens_after_env_command(
    subtests: pytest.Subtests,
) -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="claude",
        role="executor",
        requested_reasoning="high",
    )
    assignment = "CLAUDE_CODE_EFFORT_LEVEL=low"
    configs = (
        AgentConfig(
            cli_cmd=["env", "claude", assignment],
            provider_kind="claude",
        ),
        AgentConfig(
            cli_cmd=["claude"],
            extra_args=[assignment],
            provider_kind="claude",
        ),
    )

    with patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}):
        for config in configs:
            with subtests.test(config=config):
                plan = build_cli_invocation_plan(
                    config,
                    model=None,
                    prompt="prompt",
                    output_file=Path("output.md"),
                    invocation_context=context,
                )
                assert assignment in plan.cmd
                assert "--effort" in plan.cmd


def test_reasoning_request_allows_env_to_remove_inherited_effort(
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
            cli_cmd=["env", "-i", "claude"],
            provider_kind="claude",
        ),
        AgentConfig(
            cli_cmd=["env", "-u", "CLAUDE_CODE_EFFORT_LEVEL", "claude"],
            provider_kind="claude",
        ),
        AgentConfig(
            cli_cmd=[
                "env",
                "--unset=CLAUDE_CODE_EFFORT_LEVEL",
                "claude",
            ],
            provider_kind="claude",
        ),
    )

    with patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": "low"}):
        for config in configs:
            with subtests.test(config=config):
                plan = build_cli_invocation_plan(
                    config,
                    model=None,
                    prompt="prompt",
                    output_file=Path("output.md"),
                    invocation_context=context,
                )
                assert "--effort" in plan.cmd


def test_reasoning_request_rejects_clustered_env_assignment() -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="claude",
        role="executor",
        requested_reasoning="high",
    )
    config = AgentConfig(
        cli_cmd=[
            "env",
            "-iv",
            "CLAUDE_CODE_EFFORT_LEVEL=low",
            "claude",
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


def test_reasoning_request_rejects_env_split_string(subtests: pytest.Subtests) -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="claude",
        role="executor",
        requested_reasoning="high",
    )
    split_value = "CLAUDE_CODE_EFFORT_LEVEL=low claude"
    cli_commands = (
        ["env", "-S", split_value],
        ["env", f"-S{split_value}"],
        ["env", "--split-string", split_value],
        ["env", f"--split-string={split_value}"],
    )

    with patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}):
        for cli_cmd in cli_commands:
            with (
                subtests.test(cli_cmd=cli_cmd),
                pytest.raises(ValueError, match="cannot be combined"),
            ):
                build_cli_invocation_plan(
                    AgentConfig(cli_cmd=cli_cmd, provider_kind="claude"),
                    model=None,
                    prompt="prompt",
                    output_file=Path("output.md"),
                    invocation_context=context,
                )


def test_reasoning_request_rejects_env_working_directory_change(
    subtests: pytest.Subtests,
) -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="claude",
        role="executor",
        requested_reasoning="high",
    )
    cli_commands = (
        ["env", "--chdir", "subdir", "claude"],
        ["env", "--chdir=subdir", "claude"],
        ["env", "-C", "subdir", "claude"],
        ["env", "-Csubdir", "claude"],
    )

    with patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}):
        for cli_cmd in cli_commands:
            with (
                subtests.test(cli_cmd=cli_cmd),
                pytest.raises(ValueError, match="cannot be combined"),
            ):
                build_cli_invocation_plan(
                    AgentConfig(cli_cmd=cli_cmd, provider_kind="claude"),
                    model=None,
                    prompt="prompt",
                    output_file=Path("output.md"),
                    invocation_context=context,
                )


def test_reasoning_request_allows_env_prefix_provider_like_tokens(
    subtests: pytest.Subtests,
) -> None:
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="claude",
        role="executor",
        requested_reasoning="high",
    )
    cli_commands = (
        ["env", "--", "claude"],
        ["env", "-u", "--effort", "claude"],
        ["env", "--unset", "--effort", "claude"],
    )

    with patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}):
        for cli_cmd in cli_commands:
            with subtests.test(cli_cmd=cli_cmd):
                plan = build_cli_invocation_plan(
                    AgentConfig(cli_cmd=cli_cmd, provider_kind="claude"),
                    model=None,
                    prompt="prompt",
                    output_file=Path("output.md"),
                    invocation_context=context,
                )
                command_index = plan.cmd.index("claude")
                assert plan.cmd[command_index + 1 : command_index + 3] == [
                    "--effort",
                    "high",
                ]

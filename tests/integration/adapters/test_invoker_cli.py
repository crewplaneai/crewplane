import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.adapters.invokers.cli import CliInvokerAdapter
from crewplane.adapters.invokers.cli_invoker import (
    build_cli_invocation_plan,
    build_cli_log_presentation,
)
from crewplane.adapters.invokers.cli_invoker.capabilities import (
    CAPABILITIES,
    CODEX_MODEL_CAPACITY_MESSAGE,
)
from crewplane.architecture.contracts import (
    SUPPORTED_PROVIDER_KINDS,
    ProviderKind,
)
from crewplane.core.config import AgentConfig, Config
from crewplane.version import SCHEMA_VERSION


def test_create_invoker_returns_default_invoker() -> None:
    adapter = CliInvokerAdapter()
    config = Config(
        version=SCHEMA_VERSION,
        agents={
            "alpha": AgentConfig(cli_cmd=["echo"], default_model="model"),
        },
    )
    invoker = adapter.create_invoker(config=config, options={})
    assert invoker.__class__.__name__ == "PlannedAgentInvoker"


def test_create_invoker_rejects_unknown_options() -> None:
    adapter = CliInvokerAdapter()
    config = Config(version=SCHEMA_VERSION, agents={})
    with pytest.raises(ValueError, match="options: \\{}"):
        adapter.create_invoker(config=config, options={"x": 1})


def test_workspace_capabilities_declare_runtime_command_runner() -> None:
    adapter = CliInvokerAdapter()

    capabilities = adapter.canonicalize_options(
        "cli",
        "crewplane.adapters.invokers.cli:CliInvokerAdapter",
    ).capabilities["workspace"]

    assert capabilities["supported"] is True
    assert capabilities["launch_mode"] == "runtime_command_runner"
    assert capabilities["honors_cwd"] is True
    assert capabilities["controlled_child_environment"] is True


def test_explicit_model_arg_warns_for_builtin_provider_kinds(
    subtests: pytest.Subtests,
) -> None:
    adapter = CliInvokerAdapter()

    for provider_kind in set(SUPPORTED_PROVIDER_KINDS) - {ProviderKind.GENERIC}:
        for model_arg in ("--custom-model", None):
            with subtests.test(
                provider_kind=provider_kind,
                model_arg=model_arg,
            ):
                config = Config(
                    version=SCHEMA_VERSION,
                    agents={
                        "alpha": AgentConfig(
                            cli_cmd=["provider"],
                            provider_kind=provider_kind,
                            model_arg=model_arg,
                        )
                    },
                )

                warnings = adapter.collect_model_arg_warnings(config)

                assert len(warnings) == 1
                assert "Agent 'alpha': remove model_arg" in warnings[0]
                assert provider_kind.value in warnings[0]


def test_model_arg_warning_ignores_omitted_and_generic_fields() -> None:
    adapter = CliInvokerAdapter()
    config = Config(
        version=SCHEMA_VERSION,
        agents={
            "builtin": AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind=ProviderKind.CODEX,
            ),
            "generic": AgentConfig(
                cli_cmd=["provider"],
                provider_kind=ProviderKind.GENERIC,
                model_arg="--custom-model",
            ),
        },
    )

    assert adapter.collect_model_arg_warnings(config) == ()


def test_model_arg_warnings_are_sorted_by_agent_name() -> None:
    adapter = CliInvokerAdapter()
    config = Config(
        version=SCHEMA_VERSION,
        agents={
            agent_name: AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind=ProviderKind.CODEX,
                model_arg="--custom-model",
            )
            for agent_name in ("zeta", "alpha")
        },
    )

    warnings = adapter.collect_model_arg_warnings(config)

    assert [message.split("'")[1] for message in warnings] == ["alpha", "zeta"]


def test_builtin_provider_log_presentation_descriptors() -> None:
    claude = build_cli_log_presentation(
        AgentConfig(cli_cmd=["claude"], provider_kind="claude")
    )
    codex = build_cli_log_presentation(
        AgentConfig(cli_cmd=["codex"], provider_kind="codex")
    )
    gemini = build_cli_log_presentation(
        AgentConfig(cli_cmd=["gemini"], provider_kind="gemini")
    )
    kilo = build_cli_log_presentation(
        AgentConfig(cli_cmd=["kilo", "run"], provider_kind="kilo")
    )
    generic = build_cli_log_presentation(AgentConfig(cli_cmd=["echo"]))

    assert (claude.format, claude.profile) == ("json_object", "claude")
    assert (codex.format, codex.profile) == ("json_lines", "codex")
    assert (gemini.format, gemini.profile) == ("json_object", "gemini")
    assert (kilo.format, kilo.profile) == ("json_lines", "kilo")
    assert (generic.format, generic.profile) == ("plain", "generic")


def test_builtin_provider_capabilities_cover_supported_provider_kinds() -> None:
    assert set(CAPABILITIES) == set(SUPPORTED_PROVIDER_KINDS)


def test_machine_readable_provider_capabilities_supply_decoder_and_extractor() -> None:
    for provider in (ProviderKind.CLAUDE, ProviderKind.CODEX):
        capability = CAPABILITIES[provider]
        assert capability.output_extractor is not None
        assert capability.usage_decoder is not None
    for provider in (ProviderKind.GEMINI, ProviderKind.KILO):
        capability = CAPABILITIES[provider]
        assert capability.output_extractor is not None
        assert capability.usage_decoder is not None
    for provider in (ProviderKind.COPILOT, ProviderKind.GENERIC):
        capability = CAPABILITIES[provider]
        assert capability.output_extractor is None
        assert capability.usage_decoder is None


def test_gemini_and_kilo_plans_enable_machine_readable_output() -> None:
    with patch.dict(os.environ, {"PATH": ""}):
        gemini_plan = build_cli_invocation_plan(
            AgentConfig(cli_cmd=["gemini"], provider_kind="gemini"),
            model=None,
            prompt="prompt",
            output_file=Path("output.md"),
        )
        kilo_plan = build_cli_invocation_plan(
            AgentConfig(cli_cmd=["kilo", "run"], provider_kind="kilo"),
            model="kilo/kilo-auto/frontier",
            prompt="prompt",
            output_file=Path("output.md"),
        )

    assert gemini_plan.cmd == ["gemini", "--output-format", "json"]
    assert kilo_plan.cmd == [
        "kilo",
        "run",
        "--model",
        "kilo/kilo-auto/frontier",
        "--format",
        "json",
    ]
    assert gemini_plan.structured_output_mode == "gemini_json"
    assert kilo_plan.structured_output_mode == "kilo_json"
    assert not gemini_plan.supports_output_idle_timeout
    assert kilo_plan.supports_output_idle_timeout


def test_only_gemini_disables_output_idle_timeout(subtests: pytest.Subtests) -> None:
    assert not CAPABILITIES[ProviderKind.GEMINI].supports_output_idle_timeout
    for provider in set(SUPPORTED_PROVIDER_KINDS) - {ProviderKind.GEMINI}:
        with subtests.test(provider=provider):
            assert CAPABILITIES[provider].supports_output_idle_timeout


def test_codex_capability_owns_one_shot_capacity_retry() -> None:
    policy = CAPABILITIES[ProviderKind.CODEX].one_shot_failure_retry

    assert policy is not None
    assert policy is not None
    assert policy.output_contains == (CODEX_MODEL_CAPACITY_MESSAGE,)
    assert policy.wait_seconds == 5.0
    assert CAPABILITIES[ProviderKind.GENERIC].one_shot_failure_retry is None


def test_invocation_plan_resolves_cli_executable_before_workspace_cwd() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tool_dir = Path(tmp_dir) / "tools"
        tool_dir.mkdir()
        executable = tool_dir / "provider"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        expected_executable = executable.resolve(strict=True).as_posix()
        workspace_dir = Path(tmp_dir) / "workspace"
        workspace_dir.mkdir()
        (workspace_dir / "provider").write_text(
            "#!/bin/sh\nexit 99\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"PATH": tool_dir.as_posix()}):
            plan = build_cli_invocation_plan(
                AgentConfig(cli_cmd=["provider"]),
                model=None,
                prompt="prompt",
                output_file=workspace_dir / "output.md",
            )

    assert plan.cmd[0] == expected_executable


def test_invocation_plan_preserves_missing_bare_cli_executable() -> None:
    with patch.dict(os.environ, {"PATH": ""}):
        plan = build_cli_invocation_plan(
            AgentConfig(cli_cmd=["missing-provider"]),
            model=None,
            prompt="prompt",
            output_file=Path("output.md"),
        )

    assert plan.cmd[0] == "missing-provider"


def test_invocation_plan_preserves_relative_path_cli_executable() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tool_dir = Path(tmp_dir) / "tools"
        tool_dir.mkdir()
        executable = tool_dir / "provider"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        relative_executable = os.path.relpath(executable, Path.cwd())
        config = AgentConfig(cli_cmd=["echo"]).model_copy(
            update={"cli_cmd": [relative_executable]}
        )

        plan = build_cli_invocation_plan(
            config,
            model=None,
            prompt="prompt",
            output_file=Path("output.md"),
        )

    assert plan.cmd[0] == relative_executable

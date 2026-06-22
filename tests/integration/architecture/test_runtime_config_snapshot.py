from pathlib import Path

import pytest
from rich.console import Console

from orchestrator_cli.architecture.contracts import (
    CanonicalIntegrationConfig,
    InvokerAdapterCapabilities,
    InvokerWorkspaceSupport,
)
from orchestrator_cli.bootstrap import build_runtime_config_snapshot
from orchestrator_cli.bootstrap.runtime_config import normalize_invoker_capabilities
from orchestrator_cli.core.config import (
    AgentConfig,
    Config,
    IntegrationsConfig,
    IntegrationSpec,
    Settings,
)
from orchestrator_cli.core.preflight.runtime_config import (
    RuntimeAgentConfigSnapshot,
    RuntimeConfigSnapshot,
)
from orchestrator_cli.version import SCHEMA_VERSION


def _config(
    artifact_options: dict[str, object] | None = None,
    agent_config: AgentConfig | None = None,
    workspace: dict[str, object] | None = None,
) -> Config:
    selected_agent_config = (
        AgentConfig(cli_cmd=["mock"]) if agent_config is None else agent_config
    )
    return Config(
        version=SCHEMA_VERSION,
        agents={"alpha": selected_agent_config},
        settings=Settings(
            workspace=workspace or {},
            integrations=IntegrationsConfig(
                invoker=IntegrationSpec(
                    implementation="mock",
                    options={"output_mode": "echo"},
                ),
                artifacts=IntegrationSpec(
                    implementation="filesystem",
                    options=(
                        {"allowed_template_paths": [], "log_cli_output": True}
                        if artifact_options is None
                        else artifact_options
                    ),
                ),
                ui=IntegrationSpec(implementation="none", options={}),
            ),
        ),
    )


def _snapshot_for_agent(agent_config: AgentConfig) -> RuntimeConfigSnapshot:
    return build_runtime_config_snapshot(
        config=_config(agent_config=agent_config),
        console=Console(file=None),
        no_live=True,
    ).snapshot


def test_observer_only_no_live_is_excluded_from_runtime_signature() -> None:
    console = Console(file=None)
    first = build_runtime_config_snapshot(
        config=_config(),
        console=console,
        no_live=False,
    ).snapshot
    second = build_runtime_config_snapshot(
        config=_config(),
        console=console,
        no_live=True,
    ).snapshot

    assert (
        first.effective_runtime_config_signature
        == second.effective_runtime_config_signature
    )
    assert first.observer.no_live is False
    assert second.observer.no_live is True
    assert first.redacted_payload()["observer"]["no_live"] is False
    assert second.redacted_payload()["observer"]["no_live"] is True


def test_disabled_workspace_settings_are_runtime_snapshot_equivalent() -> None:
    default_snapshot = build_runtime_config_snapshot(
        config=_config(),
        console=Console(file=None),
        no_live=True,
    ).snapshot
    disabled_snapshot = build_runtime_config_snapshot(
        config=_config(
            workspace={
                "enabled": False,
                "cache_root": "/tmp/ignored-orchestrator-cache",
                "cleanup_on_success": False,
            }
        ),
        console=Console(file=None),
        no_live=True,
    ).snapshot

    assert (
        default_snapshot.effective_runtime_config_signature
        == disabled_snapshot.effective_runtime_config_signature
    )
    assert (
        default_snapshot.redacted_payload()["workspace"]
        == disabled_snapshot.redacted_payload()["workspace"]
    )


def test_agent_snapshot_preserves_default_disabled_invocation_timeout() -> None:
    config = _config()

    snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=True,
    ).snapshot

    redacted_agent = snapshot.redacted_payload()["agents"]["alpha"]
    assert snapshot.agents["alpha"].invocation_timeout_seconds is None
    assert (
        RuntimeAgentConfigSnapshot.model_validate(
            redacted_agent
        ).invocation_timeout_seconds
        is None
    )


def test_agent_snapshot_ignores_explicit_null_matching_default() -> None:
    default_snapshot = _snapshot_for_agent(AgentConfig(cli_cmd=["mock"]))
    explicit_snapshot = _snapshot_for_agent(
        AgentConfig(cli_cmd=["mock"], invocation_timeout_seconds=None)
    )

    assert (
        default_snapshot.effective_runtime_config_signature
        == explicit_snapshot.effective_runtime_config_signature
    )
    assert (
        "invocation_timeout_seconds"
        not in explicit_snapshot.redacted_payload()["agents"]["alpha"]
    )


def test_agent_snapshot_preserves_explicit_null_that_changes_default() -> None:
    default_snapshot = _snapshot_for_agent(AgentConfig(cli_cmd=["mock"]))
    disabled_idle_snapshot = _snapshot_for_agent(
        AgentConfig(cli_cmd=["mock"], invocation_idle_timeout_seconds=None)
    )

    assert (
        default_snapshot.effective_runtime_config_signature
        != disabled_idle_snapshot.effective_runtime_config_signature
    )
    assert (
        disabled_idle_snapshot.redacted_payload()["agents"]["alpha"][
            "invocation_idle_timeout_seconds"
        ]
        is None
    )


def test_snapshot_invalid_options_fail_before_artifact_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="allowed_template_paths"):
        build_runtime_config_snapshot(
            config=_config({"allowed_template_paths": "bad"}),
            console=Console(file=None),
            no_live=True,
        )

    assert not (tmp_path / ".orchestrator").exists()
    assert not (tmp_path / "execution-stages").exists()
    assert not (tmp_path / "execution-results").exists()


def test_invoker_workspace_capabilities_are_normalized_from_adapter() -> None:
    config = normalize_invoker_capabilities(
        WorkspaceCompatibleAdapter(),
        CanonicalIntegrationConfig(
            implementation="custom",
            resolved_identity="tests:WorkspaceCompatibleAdapter",
        ),
    )

    assert config.capabilities["workspace"] == {
        "supported": True,
        "launch_mode": "runtime_command_runner",
        "honors_cwd": True,
        "controlled_child_environment": True,
    }


def test_invoker_without_workspace_capabilities_defaults_to_unsupported() -> None:
    config = normalize_invoker_capabilities(
        object(),
        CanonicalIntegrationConfig(
            implementation="custom",
            resolved_identity="tests:NoWorkspaceAdapter",
        ),
    )

    assert config.capabilities["workspace"] == {
        "supported": False,
        "launch_mode": None,
        "honors_cwd": False,
        "controlled_child_environment": False,
    }


def test_conflicting_invoker_workspace_capabilities_are_rejected() -> None:
    with pytest.raises(ValueError, match="conflicts"):
        normalize_invoker_capabilities(
            WorkspaceCompatibleAdapter(),
            CanonicalIntegrationConfig(
                implementation="custom",
                resolved_identity="tests:WorkspaceCompatibleAdapter",
                capabilities={
                    "workspace": {
                        "supported": False,
                        "launch_mode": None,
                        "honors_cwd": False,
                        "controlled_child_environment": False,
                    }
                },
            ),
        )


class WorkspaceCompatibleAdapter:
    def workspace_capabilities(self) -> InvokerAdapterCapabilities:
        return InvokerAdapterCapabilities(
            workspace=InvokerWorkspaceSupport(
                supported=True,
                launch_mode="runtime_command_runner",
                honors_cwd=True,
                controlled_child_environment=True,
            )
        )

from pathlib import Path

import pytest
from rich.console import Console

from orchestrator_cli.bootstrap import build_runtime_config_snapshot
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
) -> Config:
    selected_agent_config = (
        AgentConfig(cli_cmd=["mock"]) if agent_config is None else agent_config
    )
    return Config(
        version=SCHEMA_VERSION,
        agents={"alpha": selected_agent_config},
        settings=Settings(
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
            )
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

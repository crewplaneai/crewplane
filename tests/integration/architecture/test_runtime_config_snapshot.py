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
from orchestrator_cli.core.versions import (
    CONFIG_SCHEMA_VERSION,
    WORKFLOW_SCHEMA_VERSION,
)


def _config(artifact_options: dict[str, object] | None = None) -> Config:
    return Config(
        version=CONFIG_SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["mock"])},
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


def test_observer_only_no_live_is_excluded_from_runtime_signature() -> None:
    console = Console(file=None)
    first = build_runtime_config_snapshot(
        config=_config(),
        workflow_schema_version=WORKFLOW_SCHEMA_VERSION,
        console=console,
        no_live=False,
    ).snapshot
    second = build_runtime_config_snapshot(
        config=_config(),
        workflow_schema_version=WORKFLOW_SCHEMA_VERSION,
        console=console,
        no_live=True,
    ).snapshot

    assert (
        first.effective_runtime_config_signature
        == second.effective_runtime_config_signature
    )
    assert first.observer["no_live"] is False
    assert second.observer["no_live"] is True


def test_snapshot_invalid_options_fail_before_artifact_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="allowed_template_paths"):
        build_runtime_config_snapshot(
            config=_config({"allowed_template_paths": "bad"}),
            workflow_schema_version=WORKFLOW_SCHEMA_VERSION,
            console=Console(file=None),
            no_live=True,
        )

    assert not (tmp_path / ".orchestrator").exists()
    assert not (tmp_path / "execution-stages").exists()
    assert not (tmp_path / "execution-results").exists()

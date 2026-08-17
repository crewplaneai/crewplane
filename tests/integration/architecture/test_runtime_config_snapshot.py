import json
from pathlib import Path

import pytest
from rich.console import Console

from crewplane.architecture import contracts as architecture_contracts
from crewplane.architecture.contracts import (
    CanonicalIntegrationConfig,
    redacted_integration_option_value,
)
from crewplane.architecture.errors import IntegrationResolutionError
from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.config import (
    AgentConfig,
    Config,
    IntegrationsConfig,
    IntegrationSpec,
    Settings,
)
from crewplane.core.preflight.runtime_config import (
    RuntimeAgentConfigSnapshot,
    RuntimeConfigSnapshot,
    RuntimeConfigSnapshotOptions,
)
from crewplane.version import SCHEMA_VERSION


def _config(
    artifact_options: dict[str, object] | None = None,
    agent_config: AgentConfig | None = None,
    workspace: dict[str, object] | None = None,
    ui_implementation: str = "none",
    ui_options: dict[str, object] | None = None,
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
                        {"log_cli_output": True}
                        if artifact_options is None
                        else artifact_options
                    ),
                ),
                ui=IntegrationSpec(
                    implementation=ui_implementation,
                    options={} if ui_options is None else ui_options,
                ),
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
                "cache_root": "/tmp/ignored-crewplane-cache",
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

    assert not (tmp_path / ".crewplane").exists()
    assert not (tmp_path / "execution-stages").exists()
    assert not (tmp_path / "execution-results").exists()


class LeakyInvokerAdapter:
    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: dict[str, object] | None = None,
    ) -> CanonicalIntegrationConfig:
        del implementation, resolved_identity
        raise ValueError(f"invalid adapter options: {options!r}")

    def create_invoker(
        self, config: Config, options: dict[str, object] | None = None
    ) -> object:
        del config, options
        raise AssertionError("canonicalization must fail first")


class OwnedAllowlistArtifactsAdapter:
    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: dict[str, object] | None = None,
    ) -> CanonicalIntegrationConfig:
        resolved_options = dict(options or {})
        allowed_template_paths = resolved_options.pop("allowed_template_paths")
        if resolved_options:
            raise ValueError("unexpected adapter options")
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            options={"allowed_template_paths": allowed_template_paths},
            option_scopes={"allowed_template_paths": "validation"},
        )

    def create_store(
        self,
        workflow_name: str,
        state_dir: Path,
        project_root: Path,
        options: dict[str, object] | None = None,
    ) -> object:
        del workflow_name, state_dir, project_root, options
        raise AssertionError("snapshot construction must not create the store")

    def create_terminal_history_reader(
        self,
        state_dir: Path,
        options: dict[str, object] | None = None,
    ) -> object:
        del state_dir, options
        raise AssertionError("snapshot construction must not create history readers")


def test_dotted_artifact_adapter_non_policy_allowlist_option_stays_owned() -> None:
    settings = Settings.model_validate(
        {
            "integrations": {
                "artifacts": {
                    "implementation": f"{__name__}:OwnedAllowlistArtifactsAdapter",
                    "options": {"allowed_template_paths": "adapter-owned"},
                },
                "invoker": {
                    "implementation": "mock",
                    "options": {"output_mode": "echo"},
                },
                "ui": {"implementation": "none", "options": {}},
            },
        }
    )
    config = Config(
        version=SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["mock"])},
        settings=settings,
    )

    snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=True,
    ).snapshot

    assert snapshot.file_access.allowed_template_paths == []
    assert snapshot.artifacts.options == {"allowed_template_paths": "adapter-owned"}


def test_dotted_adapter_mixed_allowlist_option_stays_adapter_owned() -> None:
    settings = Settings.model_validate(
        {
            "integrations": {
                "artifacts": {
                    "implementation": f"{__name__}:OwnedAllowlistArtifactsAdapter",
                    "options": {"allowed_template_paths": ["/tmp", 1]},
                },
                "invoker": {
                    "implementation": "mock",
                    "options": {"output_mode": "echo"},
                },
                "ui": {"implementation": "none", "options": {}},
            },
        }
    )
    config = Config(
        version=SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["mock"])},
        settings=settings,
    )

    snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=True,
    ).snapshot

    assert snapshot.file_access.allowed_template_paths == []
    assert snapshot.artifacts.options == {"allowed_template_paths": ["/tmp", 1]}


def test_adapter_option_validation_errors_do_not_expose_adapter_details() -> None:
    config = _config()
    assert config.settings is not None
    config.settings.integrations.invoker = IntegrationSpec(
        implementation=f"{__name__}:LeakyInvokerAdapter",
        options={
            "credentials": [{"api_token": 'adapter-"secret'}],
        },
    )

    with pytest.raises(ValueError) as exc_info:
        build_runtime_config_snapshot(
            config=config,
            console=Console(file=None),
            no_live=True,
        )

    assert 'adapter-"secret' not in str(exc_info.value)
    assert str(exc_info.value) == (
        "Failed to canonicalize invoker integration; adapter validation failed."
    )
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_snapshot_validates_inactive_ui_configuration() -> None:
    with pytest.raises(ValueError, match="none ui implementation does not support"):
        build_runtime_config_snapshot(
            config=_config(ui_options={"unsupported": True}),
            console=Console(file=None),
            no_live=True,
        )


def test_snapshot_resolves_inactive_ui_implementation() -> None:
    with pytest.raises(IntegrationResolutionError, match="Unknown ui implementation"):
        build_runtime_config_snapshot(
            config=_config(ui_implementation="missing"),
            console=Console(file=None),
            no_live=True,
        )


def test_canonical_options_require_exact_signature_scopes() -> None:
    with pytest.raises(ValueError, match="missing scopes"):
        CanonicalIntegrationConfig(
            implementation="custom",
            resolved_identity="example.Adapter",
            options={"behavior": "a"},
            option_scopes={},
        )
    with pytest.raises(ValueError, match="scopes without options"):
        CanonicalIntegrationConfig(
            implementation="custom",
            resolved_identity="example.Adapter",
            options={},
            option_scopes={"behavior": "execution"},
        )
    with pytest.raises(ValueError, match="options.timeout.*finite"):
        CanonicalIntegrationConfig(
            implementation="custom",
            resolved_identity="example.Adapter",
            options={"timeout": float("nan")},
            option_scopes={"timeout": "execution"},
        )


def test_canonical_options_redact_nested_pattern_and_explicit_sensitive_paths() -> None:
    config = CanonicalIntegrationConfig(
        implementation="custom",
        resolved_identity="example.Adapter",
        options={
            "routes": [
                {"api_token": "pattern-secret"},
                {"auth/config": {"value~raw": "explicit-secret"}},
            ]
        },
        sensitive_options=["/routes/1/auth~1config/value~0raw"],
        option_scopes={"routes": "execution"},
    )

    payload = config.redacted_payload()

    assert payload["sensitive_options"] == [
        "/routes/0/api_token",
        "/routes/1/auth~1config/value~0raw",
    ]
    routes = payload["options"]["routes"]
    assert routes[0]["api_token"] == {"redacted": True}
    assert routes[1]["auth/config"]["value~raw"] == {"redacted": True}
    assert "pattern-secret" not in json.dumps(payload, sort_keys=True)
    assert "explicit-secret" not in json.dumps(payload, sort_keys=True)
    assert "pattern-secret" not in repr(config)
    assert "explicit-secret" not in repr(config)
    assert architecture_contracts.sensitive_integration_option_keys(config) == {
        "routes"
    }
    assert "sensitive_integration_option_keys" in architecture_contracts.__all__


def test_canonical_options_reject_legacy_sensitive_top_level_names() -> None:
    with pytest.raises(ValueError, match="must start with '/'"):
        CanonicalIntegrationConfig(
            implementation="custom",
            resolved_identity="example.Adapter",
            options={"api_token": "legacy-secret"},
            sensitive_options=["api_token"],
            option_scopes={"api_token": "execution"},
        )


def test_canonical_options_support_json_pointer_escaped_top_level_names() -> None:
    raw_secret = "slash-prefixed-secret"
    config = CanonicalIntegrationConfig(
        implementation="custom",
        resolved_identity="example.Adapter",
        options={
            "/opaque": raw_secret,
            "opaque": "public",
            "/~1opaque": "escaped-public",
        },
        sensitive_options=["/~1opaque"],
        option_scopes={
            "/opaque": "execution",
            "opaque": "execution",
            "/~1opaque": "execution",
        },
    )

    payload = config.redacted_payload()
    round_tripped = CanonicalIntegrationConfig.model_validate(
        config.model_dump(mode="python")
    )
    neutral_integration = CanonicalIntegrationConfig(
        implementation="custom",
        resolved_identity="example.NeutralAdapter",
    )
    snapshot = RuntimeConfigSnapshot.build(
        config=_config(),
        invoker=round_tripped,
        artifacts=neutral_integration,
        ui=neutral_integration,
        options=RuntimeConfigSnapshotOptions(no_live=True),
    ).with_sensitive_config_fingerprints(b"f" * 32)

    assert config.sensitive_options == ["/~1opaque"]
    assert payload["sensitive_options"] == ["/~1opaque"]
    assert payload["options"]["/opaque"] == {"redacted": True}
    assert payload["options"]["opaque"] == "public"
    assert payload["options"]["/~1opaque"] == "escaped-public"
    assert round_tripped.sensitive_options == ["/~1opaque"]
    assert round_tripped.redacted_payload() == payload
    assert raw_secret not in json.dumps(payload, sort_keys=True)
    assert snapshot.invoker.sensitive_options == ["/~1opaque"]
    assert snapshot.invoker.options["/opaque"]["redacted"] is True
    assert snapshot.invoker.options["opaque"] == "public"
    assert snapshot.invoker.options["/~1opaque"] == "escaped-public"
    assert [item["path"] for item in snapshot.config_fingerprints] == [
        "/integrations/invoker/options/~1opaque"
    ]
    assert raw_secret not in snapshot.model_dump_json()


def test_canonical_options_use_unambiguous_pointer_for_slash_prefixed_key() -> None:
    slash_key_secret = "slash-key-secret"
    config = CanonicalIntegrationConfig(
        implementation="custom",
        resolved_identity="example.Adapter",
        options={
            "/routes/0/value": slash_key_secret,
            "routes": [{"value": "public"}],
        },
        sensitive_options=["/~1routes~10~1value"],
        option_scopes={"/routes/0/value": "execution", "routes": "execution"},
    )

    payload = config.redacted_payload()

    assert config.sensitive_options == ["/~1routes~10~1value"]
    assert payload["sensitive_options"] == ["/~1routes~10~1value"]
    assert payload["options"]["/routes/0/value"] == {"redacted": True}
    assert payload["options"]["routes"][0]["value"] == "public"
    assert slash_key_secret not in json.dumps(payload, sort_keys=True)


def test_redacted_integration_option_value_contains_only_redaction_metadata() -> None:
    raw_value = "external-adapter-secret"

    assert redacted_integration_option_value(raw_value) == {"redacted": True}

    redacted = redacted_integration_option_value(
        raw_value,
        "fingerprint",
        "config:/api_token",
    )

    assert redacted == {
        "redacted": True,
        "fingerprint": "fingerprint",
        "value_handle": "config:/api_token",
    }
    assert raw_value not in json.dumps(redacted, sort_keys=True)


def test_canonical_options_honors_explicit_pointer_below_sensitive_container() -> None:
    config = CanonicalIntegrationConfig(
        implementation="custom",
        resolved_identity="example.Adapter",
        options={"credentials": [{"value": "explicit-secret"}]},
        sensitive_options=["/credentials/0/value"],
        option_scopes={"credentials": "execution"},
    )

    payload = config.redacted_payload()

    assert payload["sensitive_options"] == ["/credentials/0/value"]
    assert payload["options"]["credentials"][0]["value"] == {"redacted": True}
    assert "explicit-secret" not in json.dumps(payload, sort_keys=True)


def test_canonical_options_do_not_trust_raw_redaction_metadata() -> None:
    raw_marker_fingerprint = "a" * 64
    raw_marker_handle = "config:/attacker-controlled-path"
    config = CanonicalIntegrationConfig(
        implementation="custom",
        resolved_identity="example.Adapter",
        options={
            "api_token": {
                "redacted": True,
                "fingerprint": raw_marker_fingerprint,
                "value_handle": raw_marker_handle,
            }
        },
        option_scopes={"api_token": "execution"},
    )

    payload = config.redacted_payload()

    serialized = json.dumps(payload, sort_keys=True)
    assert raw_marker_fingerprint not in serialized
    assert raw_marker_handle not in serialized
    assert payload["options"]["api_token"] == {"redacted": True}


@pytest.mark.parametrize(
    "sensitive_path",
    [
        "routes/0/value",
        "/routes/2/value",
        "/routes/01/value",
        "/routes/0/missing",
        "/routes/0/value/nested",
        "/routes/0/value~2suffix",
    ],
)
def test_canonical_options_reject_invalid_sensitive_json_pointers(
    sensitive_path: str,
) -> None:
    raw_secret = "CREWPLANE_NESTED_DIAGNOSTIC_SECRET"
    with pytest.raises(
        ValueError,
        match="sensitive option JSON Pointer",
    ) as exc_info:
        CanonicalIntegrationConfig(
            implementation="custom",
            resolved_identity="example.Adapter",
            options={"routes": [{"value": raw_secret}]},
            sensitive_options=[sensitive_path],
            option_scopes={"routes": "execution"},
        )
    assert raw_secret not in str(exc_info.value)
    assert raw_secret not in repr(exc_info.value)


def test_invalid_sensitive_json_pointer_escape_explains_valid_encoding() -> None:
    with pytest.raises(ValueError) as exc_info:
        architecture_contracts.parse_json_pointer("/value~2")

    assert str(exc_info.value) == (
        "The sensitive option JSON Pointer segment 'value~2' is invalid: "
        "'~' must be followed by '0' or '1'. "
        "Use '~0' for '~' and '~1' for '/'."
    )


def test_canonical_options_accept_duplicate_sensitive_option_pointers() -> None:
    config = CanonicalIntegrationConfig(
        implementation="custom",
        resolved_identity="example.Adapter",
        options={"api_token": "secret"},
        sensitive_options=["/api_token", "/api_token"],
        option_scopes={"api_token": "execution"},
    )

    assert config.sensitive_options == ["/api_token", "/api_token"]
    assert config.redacted_payload()["options"] == {"api_token": {"redacted": True}}


def test_command_argv_secrets_are_redacted_across_runtime_snapshot_fields() -> None:
    snapshot = build_runtime_config_snapshot(
        config=_config(
            agent_config=AgentConfig(
                cli_cmd=["provider", "--api-key", "AGENT_SECRET"],
            ),
            workspace={
                "enabled": True,
                "setup_profiles": {
                    "bootstrap": {
                        "run": [["setup", "--token=WORKSPACE_SECRET"]],
                    }
                },
            },
        ),
        console=Console(file=None),
        no_live=True,
    ).snapshot

    persisted = json.dumps(snapshot.redacted_payload(), sort_keys=True)

    assert "AGENT_SECRET" not in persisted
    assert "WORKSPACE_SECRET" not in persisted
    assert snapshot.agents["alpha"].cli_cmd[-1]["redacted"] is True
    workspace_secret = snapshot.workspace.setup_profiles["bootstrap"]["run"][0][1]
    assert workspace_secret["redacted"] is True
    assert snapshot.sensitive_config_paths == [
        "agents.alpha.cli_cmd.2",
        "workspace.setup_profiles.bootstrap.run.0.1",
    ]

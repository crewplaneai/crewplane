from __future__ import annotations

import json

import pytest

from crewplane.core.preflight.runtime_config.redaction import (
    redact_sensitive_config,
    redact_sensitive_config_with_fingerprints,
)

SECRET_FLAGS = (
    "--api-key",
    "--access-token",
    "--client-secret",
    "--credential",
    "--private-key",
    "--password",
)


@pytest.mark.parametrize("flag", SECRET_FLAGS)
@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("surface", ["argv", "cli_cmd", "extra_args", "setup"])
def test_every_command_surface_redacts_sensitive_argv_values(
    flag: str,
    inline: bool,
    surface: str,
) -> None:
    secret_value = 'CREWPLANE_TEST_SECRET_quoted"value\\unicode-雪'
    secret_token = f"{flag}={secret_value}" if inline else secret_value
    command = ["provider", secret_token] if inline else ["provider", flag, secret_value]
    payload = (
        {"workspace": {"setup_profiles": {"bootstrap": {"run": [command]}}}}
        if surface == "setup"
        else {"agents": {"alpha": {surface: command}}}
    )

    redacted, paths = redact_sensitive_config(payload, root_path=())
    serialized = json.dumps(redacted, sort_keys=True)
    redacted_command = (
        redacted["workspace"]["setup_profiles"]["bootstrap"]["run"][0]
        if surface == "setup"
        else redacted["agents"]["alpha"][surface]
    )

    assert secret_value not in serialized
    assert redacted_command[1 if inline else 2] == {"redacted": True}
    assert paths
    assert "provider" in serialized


@pytest.mark.parametrize(
    ("surface", "expected_path"),
    [
        ("cli_cmd", "agents.alpha.cli_cmd.1"),
        ("setup", "workspace.setup_profiles.bootstrap.run.0.1"),
    ],
)
def test_command_surfaces_redact_sensitive_environment_assignments(
    surface: str,
    expected_path: str,
) -> None:
    secret_value = "CREWPLANE_TEST_ENV_SECRET"
    auditable_assignment = "METADATA=provider-token-is-auditable-data"
    command = [
        "env",
        f"OPENAI_API_KEY={secret_value}",
        auditable_assignment,
        "provider",
    ]
    payload = (
        {"workspace": {"setup_profiles": {"bootstrap": {"run": [command]}}}}
        if surface == "setup"
        else {"agents": {"alpha": {surface: command}}}
    )

    redacted, paths = redact_sensitive_config(payload, root_path=())
    redacted_command = (
        redacted["workspace"]["setup_profiles"]["bootstrap"]["run"][0]
        if surface == "setup"
        else redacted["agents"]["alpha"][surface]
    )

    assert paths == [expected_path]
    assert redacted_command[1] == {"redacted": True}
    assert redacted_command[2] == auditable_assignment
    assert secret_value not in json.dumps(redacted, sort_keys=True)


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--model", "gpt-5"),
        ("--output", 'quoted "value"'),
        ("--label", "unicode-雪"),
        ("--metadata", "provider-token-is-auditable-data"),
    ],
)
def test_nonsensitive_argv_values_remain_auditable(option: str, value: str) -> None:
    payload = {"agents": {"alpha": {"cli_cmd": ["provider", option, value]}}}

    redacted, paths = redact_sensitive_config(payload, root_path=())

    assert paths == []
    assert redacted == payload


def test_agent_command_field_boundary_redacts_sensitive_split_value() -> None:
    secret_value = "CREWPLANE_TEST_CROSS_FIELD_SECRET"
    payload = {
        "alpha": {
            "cli_cmd": ["provider", "--api-key"],
            "extra_args": [secret_value],
        }
    }
    expected_path = "agents.alpha.extra_args.0"

    redacted, paths = redact_sensitive_config(payload)
    fingerprinted, fingerprints = redact_sensitive_config_with_fingerprints(
        payload,
        b"fingerprint-key",
    )

    assert paths == [expected_path]
    assert redacted["alpha"]["extra_args"][0] == {"redacted": True}
    fingerprinted_value = fingerprinted["alpha"]["extra_args"][0]
    assert fingerprinted_value["redacted"] is True
    assert fingerprinted_value["value_handle"] == f"config:{expected_path}"
    assert fingerprints == [
        {"path": expected_path, "fingerprint": fingerprinted_value["fingerprint"]}
    ]
    assert secret_value not in json.dumps(fingerprinted, sort_keys=True)

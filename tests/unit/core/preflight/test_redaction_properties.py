from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st

from crewplane.core.preflight.runtime_config.redaction import (
    redact_sensitive_config,
)

SECRET_FLAGS = (
    "--api-key",
    "--access-token",
    "--client-secret",
    "--credential",
    "--private-key",
    "--password",
)


@given(
    flag=st.sampled_from(SECRET_FLAGS),
    secret=st.text(
        alphabet=st.characters(min_codepoint=48, max_codepoint=122),
        min_size=1,
        max_size=80,
    ),
    inline=st.booleans(),
    surface=st.sampled_from(("argv", "cli_cmd", "extra_args", "setup")),
)
def test_every_command_surface_redacts_sensitive_argv_values(
    flag: str,
    secret: str,
    inline: bool,
    surface: str,
) -> None:
    secret_value = f"CREWPLANE_TEST_SECRET_{secret}"
    secret_token = f"{flag}={secret_value}" if inline else secret_value
    command = ["provider", secret_token] if inline else ["provider", flag, secret_value]
    payload = (
        {"workspace": {"setup_profiles": {"bootstrap": {"run": [command]}}}}
        if surface == "setup"
        else {"agents": {"alpha": {surface: command}}}
    )

    redacted, paths = redact_sensitive_config(payload, root_path=())
    serialized = json.dumps(redacted, sort_keys=True)

    assert secret_value not in serialized
    assert paths
    assert "provider" in serialized


@given(
    option=st.from_regex(r"--[a-z]{1,12}", fullmatch=True).filter(
        lambda value: (
            not any(
                marker in value
                for marker in (
                    "secret",
                    "token",
                    "password",
                    "passwd",
                    "api-key",
                    "apikey",
                    "credential",
                    "private",
                )
            )
        )
    ),
    value=st.text(
        alphabet=st.characters(
            blacklist_categories=("Cs",),
            blacklist_characters="\x00",
        ),
        min_size=1,
        max_size=40,
    ),
)
def test_nonsensitive_argv_values_remain_auditable(option: str, value: str) -> None:
    payload = {"agents": {"alpha": {"cli_cmd": ["provider", option, value]}}}

    redacted, paths = redact_sensitive_config(payload, root_path=())

    assert paths == []
    assert redacted == payload

from crewplane.version import SCHEMA_VERSION

SECRET = "canaryalpha_canaryomega"
SECRET_PARTS = (SECRET, "canaryalpha", "canaryomega", "987654321")
CONFIG_FAILURES = {
    "model": (
        f'version: "{SCHEMA_VERSION}"\nagents:\n  {SECRET}:\n    cli_cmd: [echo, {{token: {SECRET}}}]\n',
        "agents.<key>.cli_cmd.1",
    ),
    "unknown": (
        f'version: "{SCHEMA_VERSION}"\nagents: {{}}\nsettings: {{{SECRET}: {SECRET}}}\n',
        "settings.<key>",
    ),
    "numeric_key": (
        f'version: "{SCHEMA_VERSION}"\nagents:\n  alpha:\n    cli_cmd: [echo]\n    environment: {{987654321: {SECRET}}}\n',
        "agents.<key>.environment.<key>",
    ),
    "custom": (f'version: "{SECRET}"\nagents: {{}}\n', "version"),
    "agent_key": (
        f'version: "{SCHEMA_VERSION}"\nagents:\n  {SECRET}: {{cli_cmd: [echo]}}\n  " {SECRET} ": {{cli_cmd: [echo]}}\n',
        "agents",
    ),
    "blank_token": (
        f'version: "{SCHEMA_VERSION}"\nagents:\n  alpha:\n    cli_cmd: [echo]\n    extra_args: [{SECRET}, " "]\n',
        "extra_args cannot contain blank tokens",
    ),
    "yaml": (f'version: "{SCHEMA_VERSION}"\nagents: [{SECRET}\n', "line 3, column 1"),
    "duplicate": (
        f'version: "{SCHEMA_VERSION}"\nagents: {{{SECRET}: {SECRET}, {SECRET}: {SECRET}}}\n',
        "line 2, column",
    ),
}


def assert_no_secret(payload: str | bytes) -> None:
    text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    for part in SECRET_PARTS:
        assert part not in text

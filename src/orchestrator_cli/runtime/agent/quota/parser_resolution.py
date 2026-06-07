from __future__ import annotations

from orchestrator_cli.core.config import AgentConfig

from ..command_builder import normalize_cli_executable
from .lexicons import AUTO_QUOTA_PARSER_PROVIDER_BY_EXECUTABLE


def resolve_quota_parser(config: AgentConfig, cli_executable: str) -> str:
    if config.quota_parser != "auto":
        return config.quota_parser
    normalized = normalize_cli_executable(cli_executable)
    return AUTO_QUOTA_PARSER_PROVIDER_BY_EXECUTABLE.get(normalized, "generic")

from __future__ import annotations

from pathlib import Path

from crewplane.architecture.contracts import (
    CanonicalIntegrationConfig,
    FilesystemArtifactOptions,
    JsonObject,
    SignatureScope,
)
from crewplane.architecture.ports.artifacts import (
    ArtifactStorePort,
    TerminalHistoryReaderPort,
)
from crewplane.artifacts import OutputManager

from .terminal_history import FilesystemTerminalHistoryReader


def _parse_options(options: JsonObject | None) -> FilesystemArtifactOptions:
    resolved_options = dict(options or {})

    log_cli_output_raw = resolved_options.pop("log_cli_output", True)
    if not isinstance(log_cli_output_raw, bool):
        raise ValueError("artifacts option 'log_cli_output' must be a boolean")

    if resolved_options:
        raise ValueError(
            "Unsupported filesystem artifacts options: "
            f"{', '.join(sorted(resolved_options))}"
        )

    return FilesystemArtifactOptions(log_cli_output=log_cli_output_raw)


class FilesystemArtifactsAdapter:
    """Create the built-in filesystem-backed artifact store."""

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: JsonObject | None = None,
    ) -> CanonicalIntegrationConfig:
        parsed_options = _parse_options(options)
        canonical_options: JsonObject = {
            "log_cli_output": parsed_options.log_cli_output
        }
        option_scopes: dict[str, SignatureScope] = {"log_cli_output": "artifact"}
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            options=canonical_options,
            option_scopes=option_scopes,
        )

    def create_store(
        self,
        workflow_name: str,
        state_dir: Path,
        project_root: Path,
        options: JsonObject | None = None,
    ) -> ArtifactStorePort:
        """Build an artifact store rooted under Crewplane directory."""

        parsed_options = _parse_options(options)

        return OutputManager(
            workflow_name,
            base_dir=state_dir,
            template_base_dir=project_root,
            log_cli_output=parsed_options.log_cli_output,
        )

    def create_terminal_history_reader(
        self,
        state_dir: Path,
        options: JsonObject | None = None,
    ) -> TerminalHistoryReaderPort:
        _parse_options(options)
        return FilesystemTerminalHistoryReader(state_dir.resolve())

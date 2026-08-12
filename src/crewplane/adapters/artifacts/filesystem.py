from __future__ import annotations

from pathlib import Path
from typing import cast

from crewplane.architecture.contracts import (
    CanonicalIntegrationConfig,
    FilesystemArtifactOptions,
    JsonObject,
)
from crewplane.architecture.ports.artifacts import (
    ArtifactStorePort,
)
from crewplane.artifacts import OutputManager


def _parse_options(options: JsonObject | None) -> FilesystemArtifactOptions:
    resolved_options = dict(options or {})

    log_cli_output_raw = resolved_options.pop("log_cli_output", True)
    if not isinstance(log_cli_output_raw, bool):
        raise ValueError("artifacts option 'log_cli_output' must be a boolean")

    allowed_template_paths_raw = resolved_options.pop("allowed_template_paths", [])
    allowed_template_paths = _string_paths(allowed_template_paths_raw)

    if resolved_options:
        raise ValueError(
            "Unsupported filesystem artifacts options: "
            f"{', '.join(sorted(resolved_options))}"
        )

    return FilesystemArtifactOptions(
        log_cli_output=log_cli_output_raw,
        allowed_template_paths=tuple(
            Path(path).expanduser().resolve(strict=False).as_posix()
            for path in allowed_template_paths
        ),
    )


def _string_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(path, str) for path in value):
        raise ValueError(
            "artifacts option 'allowed_template_paths' must be a list of strings"
        )
    return tuple(path for path in value if isinstance(path, str))


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
            "allowed_template_paths": list(parsed_options.allowed_template_paths),
            "log_cli_output": parsed_options.log_cli_output,
        }
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            options=canonical_options,
            option_scopes={
                "allowed_template_paths": "artifact",
                "log_cli_output": "artifact",
            },
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

        return cast(
            ArtifactStorePort,
            OutputManager(
                workflow_name,
                base_dir=state_dir,
                template_base_dir=project_root,
                log_cli_output=parsed_options.log_cli_output,
            ),
        )

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator_cli.architecture.api_version import EXT_API_VERSION
from orchestrator_cli.architecture.ports.artifacts import (
    ArtifactStorePort,
)
from orchestrator_cli.artifacts import OutputManager
from orchestrator_cli.artifacts.manager import filesystem_manifest_exists
from orchestrator_cli.core.preflight.runtime_config import CanonicalIntegrationConfig


@dataclass(frozen=True)
class _FilesystemArtifactsOptions:
    log_cli_output: bool
    allowed_template_paths: list[str]


def _parse_options(options: Mapping[str, Any] | None) -> _FilesystemArtifactsOptions:
    resolved_options = dict(options or {})

    log_cli_output_raw = resolved_options.pop("log_cli_output", True)
    if not isinstance(log_cli_output_raw, bool):
        raise ValueError("artifacts option 'log_cli_output' must be a boolean")

    allowed_template_paths_raw = resolved_options.pop("allowed_template_paths", [])
    if not isinstance(allowed_template_paths_raw, list) or any(
        not isinstance(path, str) for path in allowed_template_paths_raw
    ):
        raise ValueError(
            "artifacts option 'allowed_template_paths' must be a list of strings"
        )

    if resolved_options:
        raise ValueError(
            "Unsupported filesystem artifacts options: "
            f"{', '.join(sorted(resolved_options))}"
        )

    return _FilesystemArtifactsOptions(
        log_cli_output=log_cli_output_raw,
        allowed_template_paths=allowed_template_paths_raw,
    )


class FilesystemArtifactsAdapter:
    """Create the built-in filesystem-backed artifact store."""

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: Mapping[str, Any] | None = None,
    ) -> CanonicalIntegrationConfig:
        parsed_options = _parse_options(options)
        canonical_options = {
            "allowed_template_paths": [
                Path(path).expanduser().resolve(strict=False).as_posix()
                for path in parsed_options.allowed_template_paths
            ],
            "log_cli_output": parsed_options.log_cli_output,
        }
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            api_version=EXT_API_VERSION,
            options=canonical_options,
            option_scopes={
                "allowed_template_paths": "artifact",
                "log_cli_output": "artifact",
            },
        )

    def workflow_signature_exists(
        self,
        workflow_name: str,
        orchestrator_dir: Path,
        options: Mapping[str, Any] | None,
        workflow_signature: str,
    ) -> bool:
        """Return whether a successful manifest exists without creating a run."""

        _parse_options(options)
        return filesystem_manifest_exists(
            orchestrator_dir,
            workflow_name,
            workflow_signature,
        )

    def create_store(
        self,
        workflow_name: str,
        orchestrator_dir: Path,
        project_root: Path,
        options: Mapping[str, Any] | None = None,
    ) -> ArtifactStorePort:
        """Build an artifact store rooted under the orchestrator directory."""

        parsed_options = _parse_options(options)

        return OutputManager(
            workflow_name,
            base_dir=orchestrator_dir,
            template_base_dir=project_root,
            log_cli_output=parsed_options.log_cli_output,
        )

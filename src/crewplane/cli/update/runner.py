from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from importlib.metadata import Distribution, PackageNotFoundError, distribution
from pathlib import Path

from rich.console import Console

from .detection import resolve_update_plan
from .types import (
    InstalledMetadata,
    UpdateContext,
    UpdateError,
    UpdatePlan,
)

_VERSION_PROBE_TIMEOUT_SECONDS = 15
_IMPORT_PACKAGE = __name__.partition(".")[0]


def installed_package_identity() -> tuple[str, str]:
    """Return the project name and version from installed distribution metadata."""
    package = _installed_distribution()
    return _package_name(package), package.version


def default_update_context() -> UpdateContext:
    package = _installed_distribution()
    return UpdateContext(
        package_name=_package_name(package),
        python_executable=Path(sys.executable),
        environment_root=Path(sys.prefix),
        metadata=_metadata_from_distribution(package),
        current_version=package.version,
        executable_lookup=shutil.which,
        command_runner=subprocess.run,
    )


def update_crewplane(
    console: Console | None = None,
    context: UpdateContext | None = None,
) -> int:
    """Delegate an update to the manager that owns the active installation."""
    active_console = console or Console()
    active_context = context or default_update_context()

    try:
        plan = resolve_update_plan(active_context)
    except KeyboardInterrupt:
        active_console.print("Update interrupted before the package manager ran.")
        return 130

    rendered_command = shlex.join(plan.command)
    active_console.print(
        f"Updating Crewplane through {plan.owner}: {rendered_command}",
        markup=False,
    )
    try:
        result = active_context.command_runner(
            list(plan.command),
            check=False,
            shell=False,
        )
    except KeyboardInterrupt:
        active_console.print(
            f"Update interrupted. Retry manually:\n  {rendered_command}",
            markup=False,
        )
        return 130
    except OSError as exc:
        raise UpdateError(
            f"Failed to start `{plan.command[0]}`: {exc}\n"
            f"Retry manually:\n  {rendered_command}"
        ) from exc

    exit_code = _normalized_exit_code(result.returncode)
    if exit_code != 0:
        active_console.print(
            f"Crewplane update failed with exit code {exit_code}.\n"
            "Crewplane did not retry, elevate, or alter the package-manager "
            "command.\nReview the package manager output, then retry under your "
            "normal package-manager and administrator policy:\n"
            f"  {rendered_command}",
            markup=False,
        )
        return exit_code

    try:
        observed_version = _observe_updated_version(active_context, plan)
    except KeyboardInterrupt:
        active_console.print(
            "The package manager completed, but version verification was interrupted. "
            "Check with `crewplane --version`.",
            markup=False,
        )
        return 130

    if observed_version == active_context.current_version:
        active_console.print(
            f"{plan.owner} completed successfully. Crewplane remains at version "
            f"{observed_version}; it may already be current under the manager's "
            "configured source and constraints.",
            markup=False,
        )
    else:
        active_console.print(
            f"Crewplane updated from version {active_context.current_version} to "
            f"{observed_version} through {plan.owner}.",
            markup=False,
        )
    return 0


def _installed_distribution() -> Distribution:
    try:
        return distribution(_IMPORT_PACKAGE)
    except PackageNotFoundError as exc:
        raise UpdateError(
            "The installed Crewplane package metadata could not be found. "
            "Reinstall Crewplane with the package manager that owns this command."
        ) from exc


def _package_name(package: Distribution) -> str:
    package_name = package.metadata.get("Name")
    if not package_name:
        raise UpdateError(
            "The installed Crewplane package metadata does not declare a project name."
        )
    return package_name


def _metadata_from_distribution(package: Distribution) -> InstalledMetadata:
    installer_text = _read_package_metadata_text(package, "INSTALLER")
    installer = installer_text.strip().lower() if installer_text is not None else None
    direct_url_text = _read_package_metadata_text(package, "direct_url.json")
    editable, direct_source = _direct_url_flags(direct_url_text)
    return InstalledMetadata(
        installer=installer or None,
        editable=editable,
        direct_source=direct_source,
    )


def _read_package_metadata_text(package: Distribution, name: str) -> str | None:
    try:
        return package.read_text(name)
    except OSError as exc:
        raise UpdateError(
            f"The installed package metadata `{name}` could not be read: {exc}"
        ) from exc
    except UnicodeError as exc:
        raise UpdateError(
            f"The installed package metadata `{name}` is not valid text."
        ) from exc


def _direct_url_flags(raw: str | None) -> tuple[bool, bool]:
    if raw is None:
        return False, False
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UpdateError(
            "The installed package metadata `direct_url.json` is not valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise UpdateError(
            "The installed package metadata `direct_url.json` must be a JSON object."
        )

    directory = payload.get("dir_info")
    editable = isinstance(directory, dict) and directory.get("editable") is True
    return editable, True


def _observe_updated_version(context: UpdateContext, plan: UpdatePlan) -> str:
    rendered_command = shlex.join(plan.verification_command)
    try:
        result = context.command_runner(
            list(plan.verification_command),
            capture_output=True,
            text=True,
            check=False,
            timeout=_VERSION_PROBE_TIMEOUT_SECONDS,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(
            "The package manager completed, but fresh-process version verification "
            f"timed out: `{rendered_command}`."
        ) from exc
    except OSError as exc:
        raise UpdateError(
            "The package manager completed, but fresh-process version verification "
            f"could not start: `{rendered_command}`: {exc}"
        ) from exc

    if result.returncode != 0:
        detail = result.stderr.strip() if isinstance(result.stderr, str) else ""
        suffix = f" {detail}" if detail else ""
        raise UpdateError(
            "The package manager completed, but fresh-process version verification "
            f"failed with exit code {result.returncode}: `{rendered_command}`.{suffix}"
        )
    return _parse_version_output(
        result.stdout,
        rendered_command,
        context.package_name,
    )


def _parse_version_output(
    stdout: str | None,
    command: str,
    package_name: str,
) -> str:
    if not isinstance(stdout, str):
        raise UpdateError(
            "The package manager completed, but fresh-process version verification "
            f"returned no text: `{command}`."
        )

    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise UpdateError(
            "The package manager completed, but fresh-process version verification "
            f"returned unexpected output: `{command}`."
        )

    tokens = lines[0].split()
    if len(tokens) == 1:
        return tokens[0]
    if len(tokens) == 2 and tokens[0].casefold() == package_name.casefold():
        return tokens[1]
    raise UpdateError(
        "The package manager completed, but fresh-process version verification "
        f"returned unexpected output: `{command}`."
    )


def _normalized_exit_code(returncode: int) -> int:
    return returncode if returncode >= 0 else 128 - returncode

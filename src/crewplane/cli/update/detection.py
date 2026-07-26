from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Final, Never

from .types import (
    UpdateCommand,
    UpdateContext,
    UpdateError,
    UpdatePlan,
)

_MANAGER_PROBE_TIMEOUT_SECONDS = 10
_VERSION_PROBE_CODE: Final = (
    "from importlib.metadata import version; import sys; print(version(sys.argv[1]))"
)
_PIPX_METADATA_NAME = "pipx_metadata.json"
_UV_RECEIPT_NAME = "uv-receipt.toml"
_SAFE_ENVIRONMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]*")


def resolve_update_plan(context: UpdateContext) -> UpdatePlan:
    """Return the owning package manager's canonical update command."""
    if context.metadata.editable:
        raise UpdateError(
            "Crewplane is installed in editable mode, so it cannot safely update "
            "itself.\nUpdate the source checkout and reinstall it with your normal "
            "development workflow."
        )

    npm_root = _npm_package_root(context.environment_root)
    pipx_metadata = context.environment_root / _PIPX_METADATA_NAME
    homebrew_prefix = _homebrew_prefix(
        context.environment_root,
        context.package_name,
    )
    uv_receipt = context.environment_root / _UV_RECEIPT_NAME
    markers = [
        name
        for name, present in (
            ("npm", npm_root is not None),
            ("pipx", pipx_metadata.is_file()),
            ("Homebrew", homebrew_prefix is not None),
            ("uv", uv_receipt.is_file()),
        )
        if present
    ]
    if len(markers) > 1:
        owners = ", ".join(markers)
        raise UpdateError(
            "Crewplane found conflicting package-manager ownership markers "
            f"({owners}) and will not guess which installation to mutate.\n"
            + _manual_update_guidance(context)
        )

    if npm_root is not None:
        _reject_automatic_npm_update(context, npm_root)
    if pipx_metadata.is_file():
        return _pipx_plan(context, pipx_metadata)
    if homebrew_prefix is not None:
        return _homebrew_plan(context, homebrew_prefix)
    if uv_receipt.is_file():
        return _uv_tool_plan(context)

    raise _unsupported_install_error(context)


def _reject_automatic_npm_update(
    context: UpdateContext,
    package_root: Path,
) -> Never:
    if package_root.is_symlink():
        raise UpdateError(
            "Crewplane is running from a linked npm package, which `npm update` "
            "must not replace.\nUpdate the linked source checkout instead."
        )

    package = _read_json_object(package_root / "package.json", "npm package metadata")
    if package.get("name") != context.package_name:
        raise UpdateError(
            "Crewplane is inside an npm-managed Python environment, but the "
            f"containing package is not named `{context.package_name}`.\n"
            + _manual_update_guidance(context)
        )

    _require_executable(context, "npm")
    global_root = _probe_path(context, ("npm", "root", "--global"), "npm")
    if not _same_path(package_root.parent, global_root):
        raise UpdateError(
            "Crewplane is running from a project-local npm package or an npx "
            "environment. It cannot safely update that project on its own.\n"
            "Update the project dependency, or install the global CLI with:\n"
            "  npm install --global crewplane"
        )

    raise UpdateError(
        "Crewplane's global npm package creates its private Python environment "
        "in a required postinstall script, so it cannot safely replace itself "
        "while running.\nReview and approve Crewplane's lifecycle script through "
        "your normal npm policy, then run:\n"
        f"  npm update --global {context.package_name}\n"
        "If npm was already updated while that script was blocked, recover with:\n"
        f"  npm rebuild --global {context.package_name}"
    )


def _pipx_plan(context: UpdateContext, metadata_path: Path) -> UpdatePlan:
    metadata = _read_json_object(metadata_path, "pipx metadata")
    main_package = metadata.get("main_package")
    if not isinstance(main_package, dict):
        raise _invalid_pipx_metadata_error(context)

    package = main_package.get("package")
    suffix = main_package.get("suffix")
    if (
        package != context.package_name
        or suffix is not None
        and not isinstance(suffix, str)
    ):
        raise _invalid_pipx_metadata_error(context)

    environment_name = f"{context.package_name}{suffix or ''}"
    recorded_environment = metadata.get("environment", environment_name)
    if (
        _SAFE_ENVIRONMENT_NAME.fullmatch(environment_name) is None
        or environment_name != context.environment_root.name
        or recorded_environment != environment_name
    ):
        raise _invalid_pipx_metadata_error(context)

    _require_executable(context, "pipx")
    local_home = _probe_path(
        context,
        ("pipx", "environment", "--value", "PIPX_HOME"),
        "pipx",
    )
    if _same_path(context.environment_root, local_home / "venvs" / environment_name):
        command: UpdateCommand = ("pipx", "upgrade", environment_name)
    else:
        global_home = _probe_path(
            context,
            ("pipx", "environment", "--value", "PIPX_GLOBAL_HOME"),
            "pipx",
        )
        if not _same_path(
            context.environment_root,
            global_home / "venvs" / environment_name,
        ):
            raise UpdateError(
                "pipx does not report this Crewplane environment under its local "
                "or global PIPX_HOME, so Crewplane will not update a different "
                "environment.\n" + _manual_update_guidance(context)
            )
        command = ("pipx", "upgrade", "--global", environment_name)

    return UpdatePlan(
        owner="pipx",
        command=command,
        verification_command=_python_version_command(context),
    )


def _homebrew_plan(context: UpdateContext, cellar_prefix: Path) -> UpdatePlan:
    _require_executable(context, "brew")
    reported_prefix = _probe_path(
        context,
        ("brew", "--prefix", context.package_name),
        "Homebrew",
    )
    if not _same_path(cellar_prefix, reported_prefix):
        raise UpdateError(
            "Homebrew reports a different Crewplane formula prefix, so Crewplane "
            "will not update a different installation.\n"
            + _manual_update_guidance(context)
        )

    return UpdatePlan(
        owner="Homebrew",
        command=("brew", "upgrade", context.package_name),
        verification_command=(
            str(reported_prefix / "bin" / context.package_name),
            "--version",
        ),
    )


def _uv_tool_plan(context: UpdateContext) -> UpdatePlan:
    _require_executable(context, "uv")
    tool_dir = _probe_path(context, ("uv", "tool", "dir"), "uv")
    expected_environment = tool_dir / context.package_name
    if not _same_path(context.environment_root, expected_environment):
        raise UpdateError(
            "uv reports a different Crewplane tool environment, so Crewplane "
            "will not update a different installation.\n"
            + _manual_update_guidance(context)
        )

    return UpdatePlan(
        owner="uv tool",
        command=("uv", "tool", "upgrade", context.package_name),
        verification_command=_python_version_command(context),
    )


def _unsupported_install_error(context: UpdateContext) -> UpdateError:
    if context.metadata.direct_source:
        return UpdateError(
            "Crewplane was installed from a direct URL or local source. Repeat the "
            "original install command to preserve that source instead of switching "
            "repositories implicitly."
        )

    if context.metadata.installer == "pip":
        command = (
            str(context.python_executable),
            "-m",
            "pip",
            "install",
            "--upgrade",
            context.package_name,
        )
        return UpdateError(
            "Crewplane is installed directly in a Python environment. Update that "
            "environment explicitly with:\n"
            f"  {shlex.join(command)}"
        )

    if context.metadata.installer == "uv":
        command = (
            "uv",
            "pip",
            "install",
            "--python",
            str(context.python_executable),
            "--upgrade",
            context.package_name,
        )
        return UpdateError(
            "Crewplane is installed directly in a uv-managed Python environment. "
            "Update that environment explicitly with:\n"
            f"  {shlex.join(command)}"
        )

    return UpdateError(
        "Crewplane could not confidently identify the package manager that owns "
        "this installation.\n" + _manual_update_guidance(context)
    )


def _manual_update_guidance(context: UpdateContext) -> str:
    pip_command = (
        str(context.python_executable),
        "-m",
        "pip",
        "install",
        "--upgrade",
        context.package_name,
    )
    return "\n".join(
        [
            "Update it with the same package manager used to install it:",
            f"  uv tool upgrade {context.package_name}",
            f"  pipx upgrade {context.package_name}",
            f"  brew upgrade {context.package_name}",
            f"  npm update --global {context.package_name}",
            f"  {shlex.join(pip_command)}",
        ]
    )


def _npm_package_root(environment_root: Path) -> Path | None:
    if environment_root.name != ".venv":
        return None
    package_root = environment_root.parent
    if not (package_root / "package.json").is_file():
        return None
    return package_root


def _homebrew_prefix(
    environment_root: Path,
    package_name: str,
) -> Path | None:
    for candidate in (environment_root, *environment_root.parents):
        if (
            candidate.parent.name == package_name
            and candidate.parent.parent.name == "Cellar"
        ):
            return candidate
    return None


def _python_version_command(context: UpdateContext) -> UpdateCommand:
    return (
        str(context.python_executable),
        "-c",
        _VERSION_PROBE_CODE,
        context.package_name,
    )


def _invalid_pipx_metadata_error(context: UpdateContext) -> UpdateError:
    return UpdateError(
        "The pipx metadata does not identify this environment as the Crewplane "
        "main package, so Crewplane will not mutate it.\n"
        + _manual_update_guidance(context)
    )


def _require_executable(context: UpdateContext, executable: str) -> None:
    if context.executable_lookup(executable) is None:
        raise UpdateError(
            f"The owning package manager `{executable}` was not found on PATH.\n"
            + _manual_update_guidance(context)
        )


def _probe_path(
    context: UpdateContext,
    command: UpdateCommand,
    owner: str,
) -> Path:
    output = _probe_stdout(context, command, owner)
    if len(output.splitlines()) != 1:
        raise UpdateError(
            f"`{shlex.join(command)}` returned an ambiguous path.\n"
            + _manual_update_guidance(context)
        )
    return Path(output)


def _probe_stdout(
    context: UpdateContext,
    command: UpdateCommand,
    owner: str,
) -> str:
    try:
        result = context.command_runner(
            list(command),
            capture_output=True,
            text=True,
            check=False,
            timeout=_MANAGER_PROBE_TIMEOUT_SECONDS,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(
            f"{owner} ownership probe timed out: `{shlex.join(command)}`.\n"
            + _manual_update_guidance(context)
        ) from exc
    except OSError as exc:
        raise UpdateError(
            f"Could not run {owner} ownership probe `{shlex.join(command)}`: {exc}.\n"
            + _manual_update_guidance(context)
        ) from exc

    if result.returncode != 0:
        detail = result.stderr.strip() if isinstance(result.stderr, str) else ""
        suffix = f" {detail}" if detail else ""
        raise UpdateError(
            f"{owner} ownership probe failed with exit code "
            f"{result.returncode}: `{shlex.join(command)}`.{suffix}\n"
            + _manual_update_guidance(context)
        )
    if not isinstance(result.stdout, str) or not result.stdout.strip():
        raise UpdateError(
            f"{owner} ownership probe returned no path: "
            f"`{shlex.join(command)}`.\n" + _manual_update_guidance(context)
        )
    return result.stdout.strip()


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise UpdateError(f"The {label} at `{path}` could not be read: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UpdateError(f"The {label} at `{path}` is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise UpdateError(f"The {label} at `{path}` must be a JSON object.")
    return payload


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except (OSError, RuntimeError):
        return False

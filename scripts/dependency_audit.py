from __future__ import annotations

import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Sequence
from pathlib import Path

AUDIT_PYTHON_VERSION = "3.13"
AUDIT_TIMEOUT_SECONDS = 300
PIP_AUDIT_VERSION = "2.10.1"
UV_EXPORT_TIMEOUT_SECONDS = 120


def audit_dependencies(root: Path) -> None:
    build_requirements = read_build_requirements(root / "pyproject.toml")
    with tempfile.TemporaryDirectory(prefix="crewplane-dependency-audit-") as temp_dir:
        temp_path = Path(temp_dir)
        locked_path = temp_path / "locked-project-dev.txt"
        build_path = temp_path / "build-system.txt"
        export_locked_requirements(root, locked_path)
        write_requirements(build_path, build_requirements)

        print("Auditing locked project and development dependencies...", flush=True)
        audit_requirements(root, locked_path)
        print("Auditing build-system dependencies...", flush=True)
        audit_requirements(root, build_path)


def read_build_requirements(pyproject_path: Path) -> tuple[str, ...]:
    with pyproject_path.open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)
    build_system = pyproject.get("build-system")
    requirements = (
        build_system.get("requires") if isinstance(build_system, dict) else None
    )
    if (
        not isinstance(requirements, list)
        or not requirements
        or not all(
            isinstance(requirement, str) and requirement.strip()
            for requirement in requirements
        )
    ):
        raise ValueError(
            "pyproject.toml [build-system].requires must be a non-empty list "
            "of non-empty strings"
        )
    return tuple(requirement.strip() for requirement in requirements)


def export_locked_requirements(root: Path, output_path: Path) -> None:
    subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--extra",
            "dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--output-file",
            str(output_path),
        ],
        cwd=root,
        check=True,
        timeout=UV_EXPORT_TIMEOUT_SECONDS,
    )


def write_requirements(output_path: Path, requirements: Sequence[str]) -> None:
    output_path.write_text(
        "".join(f"{requirement}\n" for requirement in requirements),
        encoding="utf-8",
    )


def audit_requirements(root: Path, requirements_path: Path) -> None:
    subprocess.run(
        [
            "uvx",
            "--python",
            AUDIT_PYTHON_VERSION,
            f"pip-audit=={PIP_AUDIT_VERSION}",
            "-r",
            str(requirements_path),
            "--strict",
        ],
        cwd=root,
        check=True,
        timeout=AUDIT_TIMEOUT_SECONDS,
    )


def main() -> int:
    try:
        audit_dependencies(Path.cwd())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Dependency audit failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

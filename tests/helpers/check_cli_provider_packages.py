"""Build and install CLI capability fixtures without checkout imports."""

import argparse
import importlib.metadata
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

if __package__:
    from .installed_package_assertions import (
        COMMAND_TIMEOUT_SECONDS,
        package_environment,
    )
else:
    from installed_package_assertions import (
        COMMAND_TIMEOUT_SECONDS,
        package_environment,
    )

REPOSITORY = Path(__file__).resolve().parents[2]


def run(command: list[str], cwd: Path, environment: dict[str, str]) -> None:
    subprocess.run(
        command, cwd=cwd, env=environment, check=True, timeout=COMMAND_TIMEOUT_SECONDS
    )


def check_incompatible_core(
    python: Path, ollama_wheel: Path, root: Path, environment: dict[str, str]
) -> None:
    fixture = REPOSITORY / "tests/fixtures/packaging/incompatible_crewplane"
    wheels = root / "incompatible-wheels"
    run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(wheels),
            str(fixture),
        ],
        root,
        environment,
    )
    result = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--dry-run",
            "--ignore-installed",
            "--no-index",
            str(next(wheels.glob("*.whl"))),
            str(ollama_wheel.resolve()),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    assert result.returncode != 0, "Ollama accepted incompatible Crewplane 0.3.7"
    detail = result.stdout + result.stderr
    assert "ResolutionImpossible" in detail, detail
    assert "crewplane>=0.3.8" in detail, detail
    print("Dependency resolver rejected Crewplane 0.3.7 for crewplane-ollama.")


def check_artifact_pair(
    artifacts: tuple[Path, Path], root: Path, environment: dict[str, str]
) -> Path:
    root.mkdir()
    venv.EnvBuilder(with_pip=True).create(root / "environment with spaces")
    binary = (
        root / "environment with spaces" / ("Scripts" if os.name == "nt" else "bin")
    )
    python = binary / ("python.exe" if os.name == "nt" else "python")
    run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            *(str(artifact.resolve()) for artifact in artifacts),
            str(next((root.parent / "wheels").glob("*.whl"))),
            f"mypy=={importlib.metadata.version('mypy')}",
            "types-PyYAML",
        ],
        root,
        environment,
    )
    execution = package_environment(
        root / "execution-environment",
        os.pathsep.join((str(binary), environment.get("PATH", os.defpath))),
    )
    for filename in (
        "assert_cli_provider_installation.py",
        "assert_ollama_agent_installation.py",
    ):
        assertions = root / filename
        shutil.copyfile(REPOSITORY / "tests/fixtures/packaging" / filename, assertions)
        run([str(python), "-I", str(assertions)], root, execution)
    shutil.copytree(
        REPOSITORY / "plugins/crewplane-ollama/src/crewplane_ollama",
        root / "consumer/crewplane_ollama",
    )
    run([str(python), "-m", "mypy", "--strict", "consumer"], root, execution)
    print(f"Installed {root.name} artifacts passed isolated acceptance.")
    return python


def check_packages(wheels: tuple[Path, Path], sdists: tuple[Path, Path]) -> None:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(
        prefix="crewplane provider packages "
    ) as temporary:
        root = Path(temporary)
        fixture = REPOSITORY / "tests/fixtures/packaging/cli_provider_plugin"
        run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--outdir",
                str(root / "wheels"),
                str(fixture),
            ],
            root,
            environment,
        )
        python = check_artifact_pair(wheels, root / "wheel", environment)
        check_incompatible_core(python, wheels[1], root, environment)
        check_artifact_pair(sdists, root / "sdist", environment)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crewplane-wheel", type=Path, required=True)
    parser.add_argument("--ollama-wheel", type=Path, required=True)
    parser.add_argument("--crewplane-sdist", type=Path, required=True)
    parser.add_argument("--ollama-sdist", type=Path, required=True)
    options = parser.parse_args()
    check_packages(
        (options.crewplane_wheel, options.ollama_wheel),
        (options.crewplane_sdist, options.ollama_sdist),
    )


if __name__ == "__main__":
    main()

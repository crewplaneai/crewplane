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

REPOSITORY = Path(__file__).resolve().parents[2]


def run(command: list[str], cwd: Path, environment: dict[str, str]) -> None:
    subprocess.run(command, cwd=cwd, env=environment, check=True, timeout=180)


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
        timeout=180,
    )
    assert result.returncode != 0, "Ollama accepted incompatible Crewplane 0.3.7"
    detail = result.stdout + result.stderr
    assert "ResolutionImpossible" in detail, detail
    assert "crewplane>=0.3.8" in detail, detail
    print("Dependency resolver rejected Crewplane 0.3.7 for crewplane-ollama.")


def check_packages(crewplane_wheel: Path, ollama_wheel: Path) -> None:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(
        prefix="crewplane-provider-packages-"
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
        venv.EnvBuilder(with_pip=True).create(root / "environment")
        binary = root / "environment" / ("Scripts" if os.name == "nt" else "bin")
        python = binary / ("python.exe" if os.name == "nt" else "python")
        check_incompatible_core(python, ollama_wheel, root, environment)
        run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                str(crewplane_wheel.resolve()),
                str(ollama_wheel.resolve()),
                str(next((root / "wheels").glob("*.whl"))),
                f"mypy=={importlib.metadata.version('mypy')}",
                "types-PyYAML",
            ],
            root,
            environment,
        )
        assertions = root / "assert_installation.py"
        shutil.copyfile(
            REPOSITORY / "tests/fixtures/packaging/assert_cli_provider_installation.py",
            assertions,
        )
        run([str(python), "-I", str(assertions)], root, environment)
        shutil.copytree(
            REPOSITORY / "plugins/crewplane-ollama/src/crewplane_ollama",
            root / "consumer/crewplane_ollama",
        )
        run([str(python), "-m", "mypy", "--strict", "consumer"], root, environment)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crewplane-wheel", type=Path, required=True)
    parser.add_argument("--ollama-wheel", type=Path, required=True)
    options = parser.parse_args()
    check_packages(options.crewplane_wheel, options.ollama_wheel)


if __name__ == "__main__":
    main()

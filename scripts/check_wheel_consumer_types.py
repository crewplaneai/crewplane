from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONSUMERS = (
    REPOSITORY_ROOT / "tests" / "typecheck" / "public_artifacts_consumer.py",
    REPOSITORY_ROOT / "tests" / "typecheck" / "public_observer_consumer.py",
)


def _venv_python(environment: Path) -> Path:
    return (
        environment / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else environment / "bin" / "python"
    )


def check_wheel(wheel: Path) -> None:
    resolved_wheel = wheel.resolve(strict=True)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = _venv_python(environment)
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                str(resolved_wheel),
            ],
            check=True,
        )
        consumers = [root / consumer.name for consumer in CONSUMERS]
        for source, destination in zip(CONSUMERS, consumers, strict=True):
            shutil.copyfile(source, destination)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "mypy",
                "--strict",
                "--python-executable",
                str(python),
                *(consumer.name for consumer in consumers),
            ],
            cwd=root,
            check=True,
        )


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        print("usage: check_wheel_consumer_types.py WHEEL", file=sys.stderr)
        return 2
    check_wheel(Path(arguments[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

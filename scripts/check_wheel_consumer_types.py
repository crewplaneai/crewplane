from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__:
    from .validation_policy import load_policy
else:
    from validation_policy import load_policy

LIMITS = load_policy().timeouts
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONSUMERS = (
    REPOSITORY_ROOT / "tests" / "typecheck" / "public_artifacts_consumer.py",
    REPOSITORY_ROOT / "tests" / "typecheck" / "public_observer_consumer.py",
    REPOSITORY_ROOT / "tests" / "typecheck" / "public_cli_provider_consumer.py",
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
        subprocess.run(
            [sys.executable, "-m", "venv", str(environment)],
            check=True,
            timeout=LIMITS.wheel_command_seconds,
        )
        python = _venv_python(environment)
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--find-links",
                str(resolved_wheel.parent),
                str(resolved_wheel),
            ],
            check=True,
            timeout=LIMITS.wheel_command_seconds,
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
            timeout=LIMITS.wheel_command_seconds,
        )


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        print("usage: check_wheel_consumer_types.py WHEEL", file=sys.stderr)
        return 2
    check_wheel(Path(arguments[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

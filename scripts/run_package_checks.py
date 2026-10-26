"""Run package acceptance commands under one deadline with owned diagnostics."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from .test_process import Deadline, run_logged
    from .validation_policy import ROOT, load_policy
else:
    from test_process import Deadline, run_logged
    from validation_policy import ROOT, load_policy


def package_commands(profile: str, root: Path = ROOT) -> Iterator[list[str]]:
    python = sys.executable
    release = [python, str(root / "scripts/release.py")]
    smoke = [python, str(root / "tests/helpers/installed_wheel_smoke.py")]
    yield [*release, "install-smoke-pip" if profile == "ci" else "package-build"]
    if profile == "ci":
        (wheel,) = (root / "dist").glob("crewplane-*.whl")
        yield [python, str(root / "scripts/check_wheel_consumer_types.py"), str(wheel)]
        for artifact in sorted((root / "dist").glob("*.whl")):
            yield [python, "-m", "zipfile", "--test", str(artifact)]
    if profile != "windows":
        artifacts = sorted((root / "dist").glob("*"))
        yield [python, "-m", "twine", "check", *map(str, artifacts)]
    if profile in ("testpypi", "windows"):
        yield [*smoke, "--installer", "pip"]
    if profile == "windows":
        yield [*smoke, "--installer", "uv"]
    if profile == "plugins":
        command = [python, str(root / "tests/helpers/check_cli_provider_packages.py")]
        for flag, pattern in (
            ("--crewplane-wheel", "crewplane-*.whl"),
            ("--ollama-wheel", "crewplane_ollama-*.whl"),
            ("--crewplane-sdist", "crewplane-*.tar.gz"),
            ("--ollama-sdist", "crewplane_ollama-*.tar.gz"),
        ):
            (artifact,) = (root / "dist").glob(pattern)
            command += [flag, str(artifact)]
        yield command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("ci", "testpypi", "windows", "plugins"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if bool(command) == bool(args.profile):
        parser.error("provide either --profile or a command after --")
    policy = load_policy()
    seconds = (
        policy.timeouts.native_package_seconds
        if args.profile == "windows"
        else policy.timeouts.package_seconds
    )
    deadline = Deadline.after(seconds)
    results = ROOT / "test-results"
    results.mkdir(exist_ok=True)
    diagnostics = Path(tempfile.mkdtemp(prefix="package-", dir=results))
    print(f"Package diagnostics: {diagnostics}", flush=True)
    outcome: dict[str, object] = {"profile": args.profile, "total_timeout": seconds}
    commands = iter([command]) if command else package_commands(args.profile)
    outcomes = []
    exit_code = 1
    try:
        for index, invocation in enumerate(commands):
            result = run_logged(
                invocation,
                ROOT,
                dict(os.environ),
                diagnostics / f"command-{index}.log",
                deadline.remaining(),
                policy.timeouts.shutdown_seconds,
            )
            outcomes.append({"command": invocation, **asdict(result)})
            exit_code = result.returncode
            if exit_code:
                break
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
        KeyboardInterrupt,
    ) as error:
        outcome["error"] = f"{type(error).__name__}: {error}"
        print(outcome["error"], file=sys.stderr)
        exit_code = 1
    finally:
        outcome["commands"] = outcomes
        exit_code = exit_code if exit_code >= 0 else 1
        outcome["exit_code"] = exit_code
        (diagnostics / "outcome.json").write_text(
            json.dumps(outcome, indent=2) + "\n", encoding="utf-8"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

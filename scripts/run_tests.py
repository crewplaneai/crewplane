"""Launch maintained pytest profiles with fresh, owned diagnostics."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import locale
import os
import platform
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__:
    from .test_process import display_text, launch_command, run_logged
else:
    from test_process import display_text, launch_command, run_logged

ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = (
    "crewplane",
    "pytest",
    "pytest-cov",
    "coverage",
    "pytest-xdist",
    "pytest-randomly",
)


@dataclass(frozen=True)
class Profile:
    workers: int = 0
    coverage: bool = False
    shuffle_seed: int | None = None
    required_git: bool = True
    marker_expression: str = ""


# Parallel defaults are enabled only where acceptance evidence is available.
PROFILES = {
    "local": Profile(
        workers=2
        if sys.platform == "linux" and sys.version_info[:2] in {(3, 13), (3, 14)}
        else 0,
        coverage=True,
        required_git=False,
    ),
    "pr-313": Profile(coverage=True, shuffle_seed=1729),
    "pr-314": Profile(coverage=True),
    "testpypi": Profile(coverage=True),
    "windows": Profile(),
    "nightly": Profile(),
    "nightly-shuffle": Profile(),
    "race": Profile(marker_expression="race"),
}


def nonnegative(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected an integer") from error
    if number < 0:
        raise argparse.ArgumentTypeError("expected a nonnegative integer")
    return number


def positive_seconds(value: str) -> float:
    import math

    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("expected finite positive seconds")
    return number


def hash_seed(value: str | None) -> int:
    if value is None or value == "random":
        return secrets.randbelow(2**32)
    number = nonnegative(value)
    if number > 2**32 - 1:
        raise argparse.ArgumentTypeError("hash seed must be between 0 and 4294967295")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, default="local")
    parser.add_argument("--workers", type=nonnegative)
    parser.add_argument("--shuffle-seed", type=nonnegative)
    parser.add_argument("--hash-seed", type=hash_seed)
    parser.add_argument("--coverage", choices=("on", "off"))
    parser.add_argument("--timeout", type=positive_seconds)
    parser.add_argument("--shutdown-timeout", type=positive_seconds, default=5)
    parser.add_argument("--statement-floor", type=float, default=96)
    parser.add_argument("--branch-floor", type=float, default=90)
    parser.add_argument("-k", default="")
    parser.add_argument(
        "-m", dest="marker_expression", help="Select tests by pytest marker expression"
    )
    parser.add_argument("selection", nargs="*")
    args = parser.parse_args(argv)
    profile = PROFILES[args.profile]
    args.workers = profile.workers if args.workers is None else args.workers
    args.coverage = profile.coverage if args.coverage is None else args.coverage == "on"
    if args.marker_expression is None:
        args.marker_expression = profile.marker_expression
    try:
        if args.shuffle_seed is None:
            args.shuffle_seed = profile.shuffle_seed
            if args.profile == "nightly-shuffle":
                args.shuffle_seed = (
                    nonnegative(os.environ["CREWPLANE_RANDOM_SEED"])
                    if "CREWPLANE_RANDOM_SEED" in os.environ
                    else secrets.randbits(64)
                )
        if args.hash_seed is None:
            args.hash_seed = hash_seed(os.environ.get("PYTHONHASHSEED"))
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))
    for floor in (args.statement_floor, args.branch_floor):
        if not 0 <= floor <= 100:
            parser.error("coverage floors must be between 0 and 100")
    return args


def pytest_command(
    args: argparse.Namespace, diagnostics: Path, root: Path = ROOT
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-c",
        str(root / "pyproject.toml"),
        f"--junitxml={diagnostics / 'junit.xml'}",
        "--durations=20",
    ]
    if args.workers:
        command += [
            "-p",
            "xdist.plugin",
            "-n",
            str(args.workers),
            "--dist=load",
            "--max-worker-restart=0",
        ]
    if args.shuffle_seed is not None:
        command += ["-p", "randomly", f"--randomly-seed={args.shuffle_seed}"]
    if args.coverage:
        command += [
            "-p",
            "pytest_cov",
            "--cov=crewplane",
            "--cov-branch",
            "--cov-report=",
            "--cov-fail-under=0",
            f"--cov-config={root / 'pyproject.toml'}",
        ]
    if args.k:
        command += ["-k", args.k]
    if args.marker_expression:
        command += ["-m", args.marker_expression]
    return [*command, *args.selection]


def environment_for(args: argparse.Namespace, diagnostics: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COVERAGE_", "COV_CORE_"))
        and key not in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")
    }
    environment.update(
        PYTHONHASHSEED=str(args.hash_seed),
        PYTHONUNBUFFERED="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    if PROFILES[args.profile].required_git:
        environment["CREWPLANE_REQUIRED_GIT"] = "1"
    if args.coverage:
        environment["COVERAGE_FILE"] = str(diagnostics / ".coverage")
        environment["COVERAGE_RCFILE"] = str(ROOT / "pyproject.toml")
    return environment


def command_output(command: list[str], root: Path) -> str | None:
    try:
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def dependency_versions() -> dict[str, str | None]:
    versions = {}
    for name in DEPENDENCIES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def reproduction_command(
    args: argparse.Namespace, root: Path = ROOT, windows: bool | None = None
) -> str:
    command = [
        sys.executable,
        str(root / "scripts/run_tests.py"),
        "--profile",
        args.profile,
        "--workers",
        str(args.workers),
        "--hash-seed",
        str(args.hash_seed),
        "--coverage",
        "on" if args.coverage else "off",
        "--statement-floor",
        str(args.statement_floor),
        "--branch-floor",
        str(args.branch_floor),
    ]
    if args.shuffle_seed is not None:
        command += ["--shuffle-seed", str(args.shuffle_seed)]
    if args.timeout is not None:
        command += ["--timeout", str(args.timeout)]
    command += ["--shutdown-timeout", str(args.shutdown_timeout)]
    if args.k:
        command += ["-k", args.k]
    command += ["-m", args.marker_expression, "--", *args.selection]
    if windows if windows is not None else os.name == "nt":

        def quote(value: str) -> str:
            return "'" + value.replace("'", "''") + "'"

        return f"Set-Location -LiteralPath {quote(str(root))}; & " + " ".join(
            map(quote, command)
        )
    return f"cd {shlex.quote(str(root))} && {shlex.join(command)}"


def metadata(
    args: argparse.Namespace, command: list[str], environment: dict[str, str]
) -> dict[str, object]:
    return {
        "source_commit": command_output(["git", "rev-parse", "HEAD"], ROOT),
        "source_dirty": command_output(["git", "status", "--porcelain"], ROOT),
        "command": launch_command(command),
        "pytest_command": command,
        "cwd": str(ROOT),
        "python": sys.version,
        "interpreter": sys.executable,
        "platform": platform.platform(),
        "runner_image": {
            key: os.environ[key]
            for key in (
                "ImageOS",
                "ImageVersion",
                "RUNNER_OS",
                "RUNNER_ARCH",
                "RUNNER_NAME",
            )
            if key in os.environ
        },
        "lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        "dependencies": dependency_versions(),
        "git": command_output(["git", "--version"], ROOT),
        "locale": locale.setlocale(locale.LC_ALL),
        "timezone": os.environ.get("TZ"),
        "local_timezone": time.tzname,
        "profile": args.profile,
        "selection": args.selection,
        "keyword": args.k,
        "marker_expression": args.marker_expression,
        "workers": args.workers,
        "shuffle_seed": args.shuffle_seed,
        "hash_seed": args.hash_seed,
        "coverage": args.coverage,
        "required_git": environment.get("CREWPLANE_REQUIRED_GIT") == "1",
        "reproduce": reproduction_command(args),
    }


def report_coverage(
    args: argparse.Namespace, diagnostics: Path, environment: dict[str, str]
) -> list[dict[str, object]]:
    if not (diagnostics / ".coverage").is_file():
        return [
            {
                "error": "Current invocation has no combined coverage data",
                "returncode": 1,
            }
        ]
    (diagnostics / "coverage.json").unlink(missing_ok=True)
    commands = [
        [
            sys.executable,
            "-m",
            "coverage",
            "report",
            "--show-missing",
            "--skip-covered",
        ],
        [
            sys.executable,
            "-m",
            "coverage",
            "json",
            "-o",
            str(diagnostics / "coverage.json"),
        ],
    ]
    outcomes = []
    for index, command in enumerate(commands):
        result = run_logged(
            command,
            ROOT,
            environment,
            diagnostics / f"coverage-{index}.log",
            timeout=120,
            shutdown_timeout=args.shutdown_timeout,
        )
        outcomes.append({"command": command, **asdict(result)})
    if (diagnostics / "coverage.json").is_file():
        command = [
            sys.executable,
            str(ROOT / "scripts/check_coverage.py"),
            str(diagnostics / "coverage.json"),
            "--statements",
            str(args.statement_floor),
            "--branches",
            str(args.branch_floor),
        ]
        result = run_logged(
            command,
            ROOT,
            environment,
            diagnostics / "coverage-gate.log",
            timeout=30,
            shutdown_timeout=args.shutdown_timeout,
        )
        outcomes.append({"command": command, **asdict(result)})
    else:
        outcomes.append(
            {"error": "Current invocation has no fresh coverage JSON", "returncode": 1}
        )
    return outcomes


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    results = ROOT / "test-results"
    results.mkdir(exist_ok=True)
    diagnostics = Path(tempfile.mkdtemp(prefix=f"{args.profile}-", dir=results))
    outcome: dict[str, object] = {}
    exit_code = 1
    try:
        command = pytest_command(args, diagnostics)
        environment = environment_for(args, diagnostics)
        details = metadata(args, command, environment)
        write_json(diagnostics / "metadata.json", details)
        display_text(f"Diagnostics: {diagnostics}\nReproduce: {details['reproduce']}\n")
        tests = run_logged(
            command,
            ROOT,
            environment,
            diagnostics / "pytest.log",
            args.timeout,
            args.shutdown_timeout,
        )
        outcome["pytest"] = asdict(tests)
        exit_code = tests.returncode
        if args.coverage:
            reports = report_coverage(args, diagnostics, environment)
            outcome["coverage"] = reports
            if any(report["returncode"] != 0 for report in reports):
                exit_code = exit_code or 1
    except (
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        KeyboardInterrupt,
    ) as error:
        outcome["error"] = f"{type(error).__name__}: {error}"
        print(outcome["error"], file=sys.stderr)
        exit_code = 1
    finally:
        log = diagnostics / "pytest.log"
        if log.exists():
            content = log.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            first_duration = next(
                (
                    index
                    for index, line in enumerate(lines)
                    if "slowest " in line and "durations" in line
                ),
                len(lines),
            )
            (diagnostics / "durations.txt").write_text(
                "\n".join(lines[first_duration:]) + "\n", encoding="utf-8"
            )
        outcome["exit_code"] = exit_code if exit_code >= 0 else 1
        write_json(diagnostics / "outcome.json", outcome)
    return int(outcome["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())

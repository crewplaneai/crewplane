"""Launch maintained pytest profiles with fresh, owned diagnostics."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

if __package__:
    from .test_process import Deadline, display_text, launch_command, run_logged
    from .validation_policy import load_policy
else:
    from test_process import Deadline, display_text, launch_command, run_logged
    from validation_policy import load_policy

ROOT = Path(__file__).resolve().parents[1]
# Load shared standard-library support without exposing the checkout on child import paths.
_environment_spec = importlib.util.spec_from_file_location(
    "execution_environment", ROOT / "tests/helpers/execution_environment.py"
)
assert _environment_spec is not None and _environment_spec.loader is not None
execution_environment = importlib.util.module_from_spec(_environment_spec)
_environment_spec.loader.exec_module(execution_environment)

DEPENDENCIES = (
    "crewplane",
    "pytest",
    "pytest-cov",
    "coverage",
    "pytest-xdist",
    "pytest-randomly",
)


POLICY = load_policy()
PROFILES = POLICY.profiles


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
    parser.add_argument(
        "--profile", choices=PROFILES, default=POLICY.runner.default_profile
    )
    parser.add_argument("--suite", choices=("core", "dots", "all"), default="core")
    parser.add_argument("--workers", type=nonnegative)
    parser.add_argument("--shuffle-seed", type=nonnegative)
    parser.add_argument("--hash-seed", type=hash_seed)
    parser.add_argument("--coverage", choices=("on", "off"))
    parser.add_argument(
        "--timeout", type=positive_seconds, help="Optional pytest-only cap in seconds"
    )
    parser.add_argument(
        "--total-timeout",
        type=positive_seconds,
        help="Total phase budget, including metadata, reports, and selected plugins",
    )
    parser.add_argument(
        "--shutdown-timeout",
        type=positive_seconds,
        default=POLICY.timeouts.shutdown_seconds,
        help=f"Grace before forced termination; final reaping allows at least {POLICY.timeouts.reap_seconds:g} seconds",
    )
    parser.add_argument(
        "--statement-floor", type=float, default=POLICY.coverage.statement_floor
    )
    parser.add_argument(
        "--branch-floor", type=float, default=POLICY.coverage.branch_floor
    )
    parser.add_argument("-k", default="")
    parser.add_argument(
        "-m", dest="marker_expression", help="Select tests by pytest marker expression"
    )
    parser.add_argument("selection", nargs="*")
    args = parser.parse_args(argv)
    profile = PROFILES[args.profile]
    platform_policy = POLICY.platform()
    default_workers = profile.workers
    if (
        profile.platform_workers
        and f"{sys.version_info.major}.{sys.version_info.minor}"
        in platform_policy.local_parallel_python
    ):
        default_workers = platform_policy.local_workers
    args.workers = default_workers if args.workers is None else args.workers
    if args.total_timeout is None:
        args.total_timeout = (
            POLICY.timeouts.race_iteration_seconds
            if args.profile == "race"
            else POLICY.timeouts.plugin_seconds
            if args.suite == "dots"
            else platform_policy.test_seconds
        )
    args.coverage = profile.coverage if args.coverage is None else args.coverage == "on"
    if args.marker_expression is None:
        args.marker_expression = profile.marker_expression
    try:
        if args.shuffle_seed is None:
            args.shuffle_seed = profile.shuffle_seed
            if profile.random_shuffle:
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
        f"--durations={POLICY.runner.duration_count}",
    ]
    if args.workers:
        command += [
            "-p",
            "xdist.plugin",
            "-n",
            str(args.workers),
            f"--dist={POLICY.runner.worker_distribution}",
            f"--max-worker-restart={POLICY.runner.max_worker_restart}",
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


def environment_for(
    args: argparse.Namespace,
    diagnostics: Path,
    root: Path,
    scenario: Mapping[str, str] | None = None,
) -> dict[str, str]:
    controls = execution_environment.validation_controls(os.environ)
    environment = execution_environment.build_execution_environment(
        root,
        execution_environment.validation_tool_path(os.environ),
        controls | dict(scenario or {}),
    )
    environment.update(
        CREWPLANE_TEST_ENV_ROOT=str(root),
        PYTHONHASHSEED=str(args.hash_seed),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    if PROFILES[args.profile].required_git:
        environment["CREWPLANE_REQUIRED_GIT"] = "1"
    if args.coverage:
        environment["COVERAGE_FILE"] = str(diagnostics / ".coverage")
        environment["COVERAGE_RCFILE"] = str(ROOT / "pyproject.toml")
    return environment


def command_output(
    command: list[str],
    root: Path,
    deadline: Deadline | None = None,
    environment: dict[str, str] | None = None,
) -> str | None:
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=deadline.remaining(POLICY.timeouts.metadata_seconds)
            if deadline
            else POLICY.timeouts.metadata_seconds,
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
    command += [
        "--shutdown-timeout",
        str(args.shutdown_timeout),
        "--total-timeout",
        str(args.total_timeout),
        "--suite",
        args.suite,
    ]
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
    args: argparse.Namespace,
    command: list[str],
    environment: dict[str, str],
    deadline: Deadline | None = None,
) -> dict[str, object]:
    return {
        "source_commit": command_output(
            ["git", "rev-parse", "HEAD"], ROOT, deadline, environment
        ),
        "source_dirty": command_output(
            ["git", "status", "--porcelain"], ROOT, deadline, environment
        ),
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
        "git": command_output(["git", "--version"], ROOT, deadline, environment),
        "locale": environment["LC_ALL"],
        "timezone": environment["TZ"],
        "execution_environment": {
            key: environment[key]
            for key in (
                "HOME",
                "XDG_CONFIG_HOME",
                "XDG_CACHE_HOME",
                "TMPDIR",
                "TERM",
                "LC_ALL",
                "TZ",
                "PYTHONHASHSEED",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
                "CREWPLANE_REQUIRED_GIT",
                "GIT_FLOOR_BIN",
                "CREWPLANE_TEST_ENV_ROOT",
            )
            if key in environment
        },
        "profile": args.profile,
        "suite": args.suite,
        "total_timeout": args.total_timeout,
        "statement_floor": args.statement_floor,
        "branch_floor": args.branch_floor,
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
    args: argparse.Namespace,
    diagnostics: Path,
    environment: dict[str, str],
    deadline: Deadline | None = None,
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
            timeout=deadline.remaining(POLICY.timeouts.coverage_report_seconds)
            if deadline
            else POLICY.timeouts.coverage_report_seconds,
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
            timeout=deadline.remaining(POLICY.timeouts.coverage_gate_seconds)
            if deadline
            else POLICY.timeouts.coverage_gate_seconds,
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


def dots_arguments(args: argparse.Namespace) -> argparse.Namespace:
    plugin_args = argparse.Namespace(**vars(args))
    plugin_args.coverage = False
    plugin_args.workers = 0
    plugin_args.shuffle_seed = None
    plugin_args.marker_expression = ""
    plugin_args.k = ""
    plugin_args.selection = ["plugins/crewplane-dots-host/tests"]
    return plugin_args


def run_dots(
    args: argparse.Namespace,
    diagnostics: Path,
    deadline: Deadline,
    environment_root: Path,
    scenario: Mapping[str, str] | None = None,
) -> list[dict[str, object]]:
    diagnostics.mkdir()
    plugin_args = dots_arguments(args)
    environment = environment_for(plugin_args, diagnostics, environment_root, scenario)
    plugin_deadline = Deadline.after(deadline.remaining(POLICY.timeouts.plugin_seconds))
    commands = [
        [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--no-deps",
            "-e",
            "plugins/crewplane-dots-host",
        ],
        pytest_command(plugin_args, diagnostics),
    ]
    outcomes = []
    for name, command in zip(("install", "pytest"), commands, strict=True):
        result = run_logged(
            command,
            ROOT,
            dict(os.environ) if name == "install" else environment,
            diagnostics / f"{name}.log",
            plugin_deadline.remaining(args.timeout if name == "pytest" else None),
            args.shutdown_timeout,
        )
        outcomes.append({"command": command, **asdict(result)})
        if result.returncode:
            break
    return outcomes


def main(
    argv: list[str] | None = None, scenario: Mapping[str, str] | None = None
) -> int:
    args = parse_args(argv)
    deadline = Deadline.after(args.total_timeout)
    results = ROOT / "test-results"
    results.mkdir(exist_ok=True)
    diagnostics = Path(tempfile.mkdtemp(prefix=f"{args.profile}-", dir=results))
    outcome: dict[str, object] = {}
    environment_root = Path(tempfile.mkdtemp(prefix="crewplane-tests-"))
    exit_code = 1
    try:
        execution_args = dots_arguments(args) if args.suite == "dots" else args
        suite_environment = environment_root / (
            "dots" if args.suite == "dots" else "core"
        )
        command = pytest_command(
            execution_args,
            diagnostics / "dots" if args.suite == "dots" else diagnostics,
        )
        environment = environment_for(
            execution_args, diagnostics, suite_environment, scenario
        )
        details = metadata(execution_args, command, environment, deadline)
        details["reproduce"] = reproduction_command(args)
        write_json(diagnostics / "metadata.json", details)
        display_text(f"Diagnostics: {diagnostics}\nReproduce: {details['reproduce']}\n")
        exit_code = 0
        if args.suite in ("core", "all"):
            tests = run_logged(
                command,
                ROOT,
                environment,
                diagnostics / "pytest.log",
                deadline.remaining(args.timeout),
                args.shutdown_timeout,
            )
            outcome["pytest"] = asdict(tests)
            exit_code = tests.returncode
            if args.coverage and not tests.interrupted:
                reports = report_coverage(args, diagnostics, environment, deadline)
                outcome["coverage"] = reports
                if any(report["returncode"] != 0 for report in reports):
                    exit_code = exit_code or 1
        if exit_code == 0 and args.suite in ("dots", "all"):
            plugins = run_dots(
                args,
                diagnostics / "dots",
                deadline,
                environment_root / "dots",
                scenario,
            )
            outcome["dots"] = plugins
            exit_code = next(
                (
                    int(result["returncode"])
                    for result in plugins
                    if result["returncode"]
                ),
                0,
            )
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
        if exit_code == 0:
            try:
                shutil.rmtree(environment_root)
            except OSError as error:
                outcome["cleanup_error"] = f"{type(error).__name__}: {error}"
                print(outcome["cleanup_error"], file=sys.stderr)
                exit_code = 1
        outcome["exit_code"] = exit_code if exit_code >= 0 else 1
        write_json(diagnostics / "outcome.json", outcome)
    return int(outcome["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())

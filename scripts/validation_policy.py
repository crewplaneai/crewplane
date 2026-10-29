"""Read repository validation defaults without third-party dependencies."""

from __future__ import annotations

import argparse
import json
import math
import sys
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Coverage:
    statement_floor: float
    branch_floor: float


@dataclass(frozen=True)
class Timeouts:
    metadata_seconds: float
    coverage_report_seconds: float
    coverage_gate_seconds: float
    plugin_seconds: float
    shutdown_seconds: float
    reap_seconds: float
    process_snapshot_seconds: float
    package_seconds: float
    package_overhead_seconds: float
    wheel_command_seconds: float
    installed_command_seconds: float
    native_package_seconds: float
    race_iteration_seconds: float
    race_overhead_seconds: float
    readiness_seconds: float
    scenario_seconds: float
    cleanup_seconds: float


@dataclass(frozen=True)
class Platform:
    test_seconds: float
    job_overhead_seconds: float
    local_workers: int
    local_parallel_python: list[str]


@dataclass(frozen=True)
class Profile:
    workers: int
    coverage: bool
    required_git: bool
    marker_expression: str
    random_shuffle: bool
    platform_workers: bool
    shuffle_seed: int | None = None


@dataclass(frozen=True)
class Runner:
    default_profile: str
    duration_count: int
    race_iterations: int
    worker_distribution: str
    max_worker_restart: int


@dataclass(frozen=True)
class Fuzz:
    total_seconds: int
    input_seconds: int
    overhead_seconds: int
    max_length: int
    rss_limit_mb: int


@dataclass(frozen=True)
class ValidationPolicy:
    coverage: Coverage
    timeouts: Timeouts
    runner: Runner
    fuzz: Fuzz
    platforms: dict[str, Platform]
    profiles: dict[str, Profile]

    def platform(self, name: str = sys.platform) -> Platform:
        try:
            return self.platforms[name]
        except KeyError as error:
            raise ValueError(f"No validation policy for platform {name!r}") from error

    def job_minutes(self) -> dict[str, int]:
        jobs = {
            name: math.ceil((value.test_seconds + value.job_overhead_seconds) / 60)
            for name, value in self.platforms.items()
        }
        limits = self.timeouts
        jobs["package"] = math.ceil(
            (limits.package_seconds + limits.package_overhead_seconds) / 60
        )
        jobs["testpypi"] = math.ceil(
            (
                self.platform("linux").test_seconds
                + limits.package_seconds
                + limits.package_overhead_seconds
            )
            / 60
        )
        jobs["race"] = math.ceil(
            (
                self.runner.race_iterations * limits.race_iteration_seconds
                + limits.race_overhead_seconds
            )
            / 60
        )
        jobs["fuzz"] = math.ceil(
            (self.fuzz.total_seconds + self.fuzz.overhead_seconds) / 60
        )
        return jobs


def settings[T: (Coverage, Timeouts, Platform, Profile, Runner, Fuzz)](
    model: type[T], values: dict[str, Any]
) -> T:
    """Reject misspelled fields and wrong TOML types at the policy boundary."""
    expected = {field.name: field for field in fields(model)}
    unknown = values.keys() - expected.keys()
    if unknown:
        raise ValueError(f"Unknown {model.__name__} settings: {sorted(unknown)}")
    for name, value in values.items():
        kind = expected[name].type
        valid = {
            "int": type(value) is int,
            "float": type(value) in (int, float),
            "bool": type(value) is bool,
            "str": isinstance(value, str),
            "list[str]": isinstance(value, list)
            and all(isinstance(item, str) for item in value),
            "int | None": type(value) is int,
        }.get(str(kind), False)
        if not valid:
            raise ValueError(f"Invalid {model.__name__}.{name}: expected {kind}")
        if type(value) in (int, float) and (not math.isfinite(value) or value < 0):
            raise ValueError(
                f"Invalid {model.__name__}.{name}: expected finite nonnegative value"
            )
        if name.endswith("seconds") and value <= 0:
            raise ValueError(
                f"Invalid {model.__name__}.{name}: expected positive seconds"
            )
    try:
        return model(**values)
    except TypeError as error:
        raise ValueError(f"Invalid {model.__name__} settings: {error}") from error


def load_policy(path: Path = ROOT / "pyproject.toml") -> ValidationPolicy:
    with path.open("rb") as source:
        document = tomllib.load(source)
    try:
        table = document["tool"]["crewplane"]["validation"]
        expected = {
            "coverage",
            "timeouts",
            "runner",
            "fuzz",
            "platforms",
            "profile_defaults",
            "profiles",
        }
        if table.keys() != expected:
            raise ValueError(f"Validation policy tables must be {sorted(expected)}")
        policy = ValidationPolicy(
            coverage=settings(Coverage, table["coverage"]),
            timeouts=settings(Timeouts, table["timeouts"]),
            runner=settings(Runner, table["runner"]),
            fuzz=settings(Fuzz, table["fuzz"]),
            platforms={
                name: settings(Platform, values)
                for name, values in table["platforms"].items()
            },
            profiles={
                name: settings(Profile, table["profile_defaults"] | values)
                for name, values in table["profiles"].items()
            },
        )
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError(f"Missing or malformed validation policy in {path}") from error
    if any(value > 100 for value in asdict(policy.coverage).values()):
        raise ValueError("Coverage floors must be between 0 and 100")
    if policy.platforms.keys() != {"linux", "darwin", "win32"}:
        raise ValueError("Validation platforms must be linux, darwin, and win32")
    if policy.runner.default_profile not in policy.profiles:
        raise ValueError("Default test profile is not defined")
    if policy.runner.race_iterations <= 0:
        raise ValueError("Race iterations must be positive")
    if (
        policy.timeouts.native_package_seconds
        >= policy.platform("win32").job_overhead_seconds
    ):
        raise ValueError(
            "Windows package checks must leave time for job setup and uploads"
        )
    return policy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--github-output", type=Path)
    output.add_argument(
        "--get", help="Print one scalar setting, such as runner.default_profile"
    )
    args = parser.parse_args(argv)
    try:
        policy = load_policy()
    except (OSError, ValueError) as error:
        parser.error(str(error))
    data = {**asdict(policy), "job_minutes": policy.job_minutes()}
    if args.get:
        value: Any = data
        try:
            for part in args.get.split("."):
                value = value[part]
        except (KeyError, TypeError) as error:
            parser.error(f"Unknown policy setting {args.get!r}: {error}")
        if not isinstance(value, (str, int, float, bool)):
            parser.error("--get requires a scalar setting")
        print(value)
        return 0
    payload = json.dumps(data, separators=(",", ":"))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"policy={payload}\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

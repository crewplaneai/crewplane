"""A phase shares its remaining time across commands and stops at exhaustion."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import run_package_checks, run_tests, test_process
from scripts.test_process import Deadline, ProcessOutcome
from scripts.validation_policy import load_policy


def test_deadline_caps_commands_without_resetting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(test_process.time, "monotonic", lambda: now[0])
    deadline = Deadline.after(30)
    assert deadline.remaining(10) == 10
    now[0] += 25
    assert deadline.remaining(10) == 5
    now[0] += 5
    with pytest.raises(TimeoutError):
        deadline.remaining()


@pytest.mark.parametrize("install_seconds", [10, 20])
def test_core_and_plugin_share_one_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, install_seconds: int
) -> None:
    now = [100.0]
    timeouts = []
    elapsed = iter([30, install_seconds, 4])

    def run(command, cwd, environment, log, timeout, shutdown_timeout):
        del command, cwd, environment, shutdown_timeout
        timeouts.append(timeout)
        seconds = next(elapsed)
        now[0] += seconds
        log.write_text("completed\n", encoding="utf-8")
        return ProcessOutcome(0, seconds)

    def metadata(*args):
        del args
        return {"reproduce": "test-owned command"}

    monkeypatch.setattr(test_process.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(run_tests, "ROOT", tmp_path)
    monkeypatch.setattr(run_tests, "metadata", metadata)
    monkeypatch.setattr(run_tests, "run_logged", run)
    result = run_tests.main(
        ["--profile", "nightly", "--suite", "all", "--total-timeout", "50"]
    )
    assert result == (0 if install_seconds == 10 else 1)
    assert timeouts == ([50, 20, 10] if install_seconds == 10 else [50, 20])
    (diagnostics,) = (tmp_path / "test-results").iterdir()
    outcome = json.loads((diagnostics / "outcome.json").read_text())
    assert outcome["pytest"]["returncode"] == 0
    if install_seconds == 20:
        assert "deadline expired" in outcome["error"]


def test_plugin_pytest_honors_the_explicit_pytest_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeouts = []

    def run(command, cwd, environment, log, timeout, shutdown_timeout):
        del command, cwd, environment, log, shutdown_timeout
        timeouts.append(timeout)
        return ProcessOutcome(0, 0)

    monkeypatch.setattr(test_process.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(run_tests, "run_logged", run)
    args = run_tests.parse_args(["--suite", "dots", "--timeout", "7"])
    run_tests.run_dots(args, tmp_path / "dots", Deadline.after(50))
    assert timeouts == [50, 7]


def test_coverage_reports_share_remaining_phase_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [100.0]
    calls = []
    (tmp_path / ".coverage").touch()

    def run(command, cwd, environment, log, timeout, shutdown_timeout):
        del command, cwd, environment, log, shutdown_timeout
        calls.append(timeout)
        now[0] += 20
        return ProcessOutcome(0, 20)

    monkeypatch.setattr(test_process.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(run_tests, "run_logged", run)
    with pytest.raises(TimeoutError):
        run_tests.report_coverage(
            run_tests.parse_args([]), tmp_path, {}, Deadline.after(20)
        )
    assert calls == [20]


def test_package_checks_stop_before_starting_a_command_after_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    policy = load_policy()
    policy = replace(policy, timeouts=replace(policy.timeouts, package_seconds=30))
    launched = []

    def commands(profile):
        del profile
        return iter([["first"], ["second"], ["third"]])

    def run(command, cwd, environment, log, timeout, shutdown_timeout):
        del cwd, environment, log, shutdown_timeout
        launched.append((command, timeout))
        now[0] += 15
        return ProcessOutcome(0, 15)

    monkeypatch.setattr(test_process.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(run_package_checks, "ROOT", tmp_path)
    monkeypatch.setattr(run_package_checks, "load_policy", lambda: policy)
    monkeypatch.setattr(run_package_checks, "package_commands", commands)
    monkeypatch.setattr(run_package_checks, "run_logged", run)
    assert run_package_checks.main(["--profile", "ci"]) == 1
    assert launched == [(["first"], 30), (["second"], 15)]
    (diagnostics,) = (tmp_path / "test-results").iterdir()
    outcome = json.loads((diagnostics / "outcome.json").read_text())
    assert "deadline expired" in outcome["error"]
    assert len(outcome["commands"]) == 2


@pytest.mark.parametrize("profile", ["ci", "testpypi", "windows", "plugins"])
def test_package_profiles_preserve_acceptance_checks(
    tmp_path: Path, profile: str
) -> None:
    commands = run_package_checks.package_commands(profile, tmp_path)
    first = next(commands)
    assert first[-1] == ("install-smoke-pip" if profile == "ci" else "package-build")
    dist = tmp_path / "dist"
    dist.mkdir()
    for package in ("crewplane", "crewplane_ollama"):
        for extension in ("whl", "tar.gz"):
            (dist / f"{package}-1.0.{extension}").touch()
    remaining = list(commands)
    if profile == "ci":
        assert remaining[0][1].endswith("check_wheel_consumer_types.py")
        assert sum("zipfile" in command for command in remaining) == 2
    if profile != "windows":
        assert any("twine" in command for command in remaining)
    installers = [command[-1] for command in remaining if "--installer" in command]
    assert installers == (
        {"windows": ["pip", "uv"], "testpypi": ["pip"]}.get(profile, [])
    )
    if profile == "plugins":
        assert remaining[-1][1].endswith("check_cli_provider_packages.py")
        assert "--crewplane-sdist" in remaining[-1]
        assert "--ollama-sdist" in remaining[-1]

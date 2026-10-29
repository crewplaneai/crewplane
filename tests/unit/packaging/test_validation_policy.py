"""Shared policy defaults, overrides, and derived job budgets."""

import json
from pathlib import Path

import pytest

from scripts import run_tests, validation_policy
from tests.unit.packaging.ci_workflow_support import load_workflow


def policy_file(tmp_path: Path, original: str, replacement: str) -> Path:
    source = (validation_policy.ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert original in source
    path = tmp_path / "pyproject.toml"
    path.write_text(source.replace(original, replacement, 1), encoding="utf-8")
    return path


def test_job_budgets_include_phase_and_overhead() -> None:
    assert validation_policy.load_policy().job_minutes() == {
        "linux": 30,
        "darwin": 45,
        "win32": 45,
        "package": 20,
        "testpypi": 45,
        "race": 10,
        "fuzz": 3,
    }


def test_budget_change_updates_every_dependent_job_and_rounds_up(
    tmp_path: Path,
) -> None:
    path = policy_file(tmp_path, "test_seconds = 1500", "test_seconds = 1501")
    jobs = validation_policy.load_policy(path).job_minutes()
    assert jobs["linux"] == 31
    assert jobs["testpypi"] == 46
    assert jobs["package"] == 20
    assert jobs["win32"] == 45


def test_race_budget_tracks_repetitions(tmp_path: Path) -> None:
    path = policy_file(tmp_path, "race_iterations = 5", "race_iterations = 6")
    assert validation_policy.load_policy(path).job_minutes()["race"] == 12


@pytest.mark.parametrize(
    "setting",
    ["metadata_seconds", "readiness_seconds", "scenario_seconds", "cleanup_seconds"],
)
@pytest.mark.parametrize("replacement", ["0", "-1", "true", '"10"', "nan", "inf"])
def test_invalid_timeout_fails_explicitly(
    tmp_path: Path, replacement: str, setting: str
) -> None:
    default = getattr(validation_policy.load_policy().timeouts, setting)
    path = policy_file(tmp_path, f"{setting} = {default}", f"{setting} = {replacement}")
    with pytest.raises(ValueError, match=setting):
        validation_policy.load_policy(path)


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("statement_floor = 96", "statement_floor = 101"),
        ("statement_floor = 96", "statement_flor = 96"),
        ("statement_floor = 96", ""),
        ("race_iterations = 5", "race_iterations = 0"),
        ('default_profile = "local"', 'default_profile = "missing"'),
        ("native_package_seconds = 300", "native_package_seconds = 600"),
    ],
)
def test_invalid_policy_cannot_fall_back_to_script_defaults(
    tmp_path: Path, original: str, replacement: str
) -> None:
    with pytest.raises(ValueError):
        validation_policy.load_policy(policy_file(tmp_path, original, replacement))


def test_python_runner_reads_changed_floor_and_preserves_cli_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = policy_file(tmp_path, "statement_floor = 96", "statement_floor = 97.5")
    monkeypatch.setattr(run_tests, "POLICY", validation_policy.load_policy(path))
    assert run_tests.parse_args([]).statement_floor == 97.5
    assert run_tests.parse_args(["--statement-floor", "95"]).statement_floor == 95


def test_export_uses_checkout_policy_from_another_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("invalid = [", encoding="utf-8")
    output = tmp_path / "github-output"
    assert validation_policy.main(["--github-output", str(output)]) == 0
    name, payload = output.read_text().strip().split("=", 1)
    assert name == "policy"
    exported = json.loads(payload)
    assert exported["job_minutes"] == validation_policy.load_policy().job_minutes()
    assert validation_policy.main(["--get", "runner.default_profile"]) == 0
    assert (
        capsys.readouterr().out.strip()
        == validation_policy.load_policy().runner.default_profile
    )


@pytest.mark.parametrize(
    ("workflow", "job", "budget"),
    [
        ("ci.yml", "test", "linux"),
        ("ci.yml", "windows", "win32"),
        ("ci.yml", "package", "package"),
        ("nightly.yml", "shuffled-suite", "linux"),
        ("nightly.yml", "focused-race-loop", "race"),
        ("testpypi.yml", "build", "testpypi"),
        ("release.yml", "verify", "package"),
    ],
)
def test_ci_jobs_consume_the_shared_policy(
    workflow: str, job: str, budget: str
) -> None:
    jobs = load_workflow(workflow)["jobs"]
    assert jobs["policy"]["uses"] == "./.github/workflows/validation-policy.yml"
    needs = jobs[job]["needs"]
    assert "policy" in (needs if isinstance(needs, list) else [needs])
    assert (
        jobs[job]["timeout-minutes"]
        == "${{ fromJSON(needs.policy.outputs.policy).job_minutes." + budget + " }}"
    )

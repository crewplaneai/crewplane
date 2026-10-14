import json
from pathlib import Path
from textwrap import dedent

from tests.helpers.fixture_files import copy_fixture
from tests.helpers.test_runner_project import run_fixture, runner_project

ROOT = Path(__file__).resolve().parents[3]


def test_runner_reports_coverage_in_fresh_processes(tmp_path: Path) -> None:
    script = runner_project(tmp_path)
    copy_fixture("test_runner/coverage_reporting.py", tmp_path / "test_example.py")
    (tmp_path / "crewplane.py").write_text(
        dedent("""\
            value = 1

            def choose(flag: bool) -> int:
                if flag:
                    return 1
                return 2
            """),
        encoding="utf-8",
    )

    result = run_fixture(script, "--profile", "local", "--workers", "0")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "Statements: 100.00%" in result.stdout
    assert "Branches: 100.00%" in result.stdout
    (diagnostics,) = (tmp_path / "test-results").iterdir()
    report = json.loads((diagnostics / "coverage.json").read_text(encoding="utf-8"))
    assert report["meta"]["branch_coverage"] is True
    assert report["totals"]["missing_lines"] == 0
    assert report["totals"]["missing_branches"] == 0
    assert report["totals"]["num_branches"] > 0


def test_failed_tests_cannot_be_overridden_by_successful_coverage(
    tmp_path: Path,
) -> None:
    script = runner_project(tmp_path)
    (tmp_path / "test_example.py").write_text(
        "import crewplane\n\n"
        "def test_bad() -> None:\n"
        "    assert crewplane.value == 2\n",
        encoding="utf-8",
    )
    (tmp_path / "crewplane.py").write_text("value = 1\n", encoding="utf-8")
    result = run_fixture(script, "--coverage", "on")
    assert result.returncode == 1
    (diagnostics,) = (tmp_path / "test-results").iterdir()
    outcome = json.loads((diagnostics / "outcome.json").read_text())
    assert outcome["pytest"]["returncode"] == 1
    assert all(report["returncode"] == 0 for report in outcome["coverage"])
    assert (diagnostics / "coverage.json").is_file()


def test_reporting_failure_is_preserved_with_data(tmp_path: Path) -> None:
    script = runner_project(tmp_path)
    (tmp_path / "test_example.py").write_text(
        "import crewplane\n\ndef test_ok() -> None:\n    assert crewplane.value == 1\n",
        encoding="utf-8",
    )
    (tmp_path / "crewplane.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "scripts/check_coverage.py").write_text(
        "raise SystemExit(9)\n", encoding="utf-8"
    )
    result = run_fixture(script, "--coverage", "on")
    assert result.returncode != 0
    (diagnostics,) = (tmp_path / "test-results").iterdir()
    outcome = json.loads((diagnostics / "outcome.json").read_text())
    assert outcome["pytest"]["returncode"] == 0
    assert outcome["coverage"][-1]["returncode"] == 9
    assert (diagnostics / ".coverage").is_file()
    assert (diagnostics / "coverage.json").is_file()


def test_child_only_path_requires_subprocess_measurement(tmp_path: Path) -> None:
    import sys

    from tests.helpers.child_coverage_probe import measure_child

    enabled = measure_child(tmp_path / "enabled", Path(sys.executable))
    disabled = measure_child(tmp_path / "disabled", Path(sys.executable), enabled=False)
    assert enabled["source"] == disabled["source"]
    assert enabled["covered"] is True
    assert disabled["covered"] is False

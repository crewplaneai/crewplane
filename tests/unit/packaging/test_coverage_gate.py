import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_coverage.py"


def run_gate(
    tmp_path: Path,
    totals: dict[str, object],
    floors: tuple[str, str] = ("93", "83"),
    branch_coverage: bool = True,
) -> subprocess.CompletedProcess[str]:
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps({"meta": {"branch_coverage": branch_coverage}, "totals": totals}),
        encoding="utf-8",
    )
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(report),
            "--statements",
            floors[0],
            "--branches",
            floors[1],
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def coverage_totals(statements: int, branches: int) -> dict[str, object]:
    return {
        "covered_lines": statements,
        "num_statements": 100,
        "covered_branches": branches,
        "num_branches": 100,
    }


def test_statement_and_branch_floors_pass_independently(tmp_path: Path) -> None:
    result = run_gate(tmp_path, coverage_totals(100, 83), ("100", "83"))

    assert result.returncode == 0, result.stderr
    assert "Statements: 100.00% (100/100), required 100%" in result.stdout
    assert "Branches: 83.00% (83/100), required 83%" in result.stdout


@pytest.mark.parametrize(
    ("statements", "branches", "failed_metric"),
    [(92, 100, "Statements"), (100, 82, "Branches")],
)
def test_one_metric_cannot_compensate_for_the_other(
    tmp_path: Path, statements: int, branches: int, failed_metric: str
) -> None:
    result = run_gate(tmp_path, coverage_totals(statements, branches))

    assert result.returncode == 1
    assert f"{failed_metric} coverage is below its threshold." in result.stderr


def test_thresholds_compare_counts_without_display_rounding(tmp_path: Path) -> None:
    totals = coverage_totals(100, 100)
    totals.update(covered_lines=99_999, num_statements=100_000)

    result = run_gate(tmp_path, totals, ("100", "83"))

    assert result.returncode == 1
    assert "Statements: 100.00%" in result.stdout


@pytest.mark.parametrize(("floor", "exit_code"), [("93.5", 0), ("93.51", 1)])
def test_decimal_thresholds_are_supported(
    tmp_path: Path, floor: str, exit_code: int
) -> None:
    totals = coverage_totals(100, 100)
    totals.update(covered_lines=935, num_statements=1000)

    result = run_gate(tmp_path, totals, (floor, "83"))

    assert result.returncode == exit_code, result.stderr


def test_decimal_precision_cannot_round_an_unmet_floor_down(tmp_path: Path) -> None:
    result = run_gate(
        tmp_path, coverage_totals(99, 100), ("99.0000000000000000000000000001", "83")
    )

    assert result.returncode == 1


@pytest.mark.parametrize("floor", ["-1", "101", "NaN", "Infinity", "invalid"])
def test_invalid_threshold_is_rejected(tmp_path: Path, floor: str) -> None:
    result = run_gate(tmp_path, coverage_totals(100, 100), (floor, "83"))

    assert result.returncode == 2
    assert "between 0 and 100" in result.stderr


@pytest.mark.parametrize(
    ("totals", "message"),
    [
        pytest.param({}, "invalid statements counts", id="missing-counts"),
        pytest.param(
            {**coverage_totals(100, 100), "covered_lines": True},
            "invalid statements counts",
            id="boolean-count",
        ),
        pytest.param(
            {**coverage_totals(100, 100), "covered_lines": 101},
            "invalid statements counts",
            id="covered-exceeds-total",
        ),
        pytest.param(
            {**coverage_totals(100, 100), "covered_branches": -1},
            "invalid branches counts",
            id="negative-count",
        ),
        pytest.param(
            {**coverage_totals(0, 100), "num_statements": 0},
            "no executable statements measured",
            id="empty-source",
        ),
    ],
)
def test_invalid_or_empty_measurement_cannot_pass(
    tmp_path: Path, totals: dict[str, object], message: str
) -> None:
    result = run_gate(tmp_path, totals)

    assert result.returncode == 2
    assert f"Invalid coverage report: {message}" in result.stderr


def test_branch_collection_is_required(tmp_path: Path) -> None:
    result = run_gate(tmp_path, coverage_totals(100, 100), branch_coverage=False)

    assert result.returncode == 2
    assert "branch coverage must be enabled" in result.stderr


def test_branch_free_source_has_complete_branch_coverage(tmp_path: Path) -> None:
    totals = coverage_totals(100, 0)
    totals["num_branches"] = 0

    result = run_gate(tmp_path, totals, ("100", "100"))

    assert result.returncode == 0, result.stderr
    assert "Branches: 100.00% (0/0)" in result.stdout


@pytest.mark.parametrize("contents", [None, "not-json", "[]"])
def test_missing_or_malformed_report_is_rejected(
    tmp_path: Path, contents: str | None
) -> None:
    report = tmp_path / "coverage.json"
    if contents is not None:
        report.write_text(contents, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(report),
            "--statements",
            "93",
            "--branches",
            "83",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 2
    assert "Invalid coverage report" in result.stderr

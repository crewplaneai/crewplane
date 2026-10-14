"""Measure a child-only Crewplane path in a controlled Python environment."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from tests.helpers.fixture_files import copy_fixture
from tests.helpers.test_runner_project import run_fixture, runner_project


def measure_child(
    root: Path, child_python: Path, enabled: bool = True
) -> dict[str, object]:
    root.mkdir()
    script = runner_project(root)
    copy_fixture("test_runner/child_coverage_parent.py", root / "test_example.py")
    copy_fixture("packaging/child_coverage.py", root / "child_coverage.py")
    (root / "child-python.json").write_text(
        json.dumps(str(child_python)), encoding="utf-8"
    )
    if not enabled:
        config = root / "pyproject.toml"
        config.write_text(
            config.read_text().replace('patch = ["subprocess"]', "patch = []")
        )
    result = run_fixture(
        script,
        "--coverage",
        "on",
        "--workers",
        "0",
        "--statement-floor",
        "0",
        "--branch-floor",
        "0",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    (invocation,) = (root / "test-results").iterdir()
    report = json.loads((invocation / "coverage.json").read_text())
    child = json.loads((root / "child.json").read_text())
    target = Path(child["source"]).resolve()
    measured = next(
        (
            data
            for filename, data in report["files"].items()
            if (root / filename).resolve() == target
        ),
        {},
    )
    covered = child["line"] in measured.get("executed_lines", [])
    assert covered is enabled, (child, measured.get("executed_lines"))
    return {
        **child,
        "subprocess_patch": enabled,
        "covered": covered,
        "diagnostics": str(invocation),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument("--installed", action="store_true")
    args = parser.parse_args()
    args.diagnostics.mkdir(parents=True, exist_ok=True)
    root = (
        Path(tempfile.mkdtemp(prefix="child-coverage-", dir=args.diagnostics))
        / "project"
    )
    evidence = measure_child(root, args.python)
    if args.installed:
        assert Path(evidence["source"]).is_relative_to(Path(evidence["prefix"]))
        assert not Path(evidence["source"]).is_relative_to(
            Path(__file__).resolve().parents[2]
        )
    (root.parent / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()

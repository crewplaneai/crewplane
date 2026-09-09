"""Enforce independent statement and branch floors from one coverage.py JSON report."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


@dataclass(frozen=True)
class CoverageMetric:
    name: str
    covered: int
    total: int

    @property
    def percentage(self) -> Decimal:
        return Decimal(self.covered) * 100 / self.total if self.total else Decimal(100)

    def meets(self, floor: Decimal) -> bool:
        numerator, denominator = floor.as_integer_ratio()
        return self.covered * 100 * denominator >= numerator * self.total


def coverage_floor(value: str) -> Decimal:
    try:
        floor = Decimal(value)
        if floor.is_finite() and 0 <= floor <= 100:
            return floor
    except InvalidOperation:
        pass
    raise argparse.ArgumentTypeError("coverage threshold must be between 0 and 100")


def read_metrics(report: Path) -> tuple[CoverageMetric, CoverageMetric]:
    payload = json.loads(report.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    metadata = payload.get("meta")
    if not isinstance(metadata, dict) or metadata.get("branch_coverage") is not True:
        raise ValueError("branch coverage must be enabled")
    totals = payload.get("totals")
    if not isinstance(totals, dict):
        raise ValueError("missing coverage totals")
    statements = read_metric("Statements", totals, ("covered_lines", "num_statements"))
    branches = read_metric("Branches", totals, ("covered_branches", "num_branches"))
    if statements.total == 0:
        raise ValueError("no executable statements measured")
    return statements, branches


def read_metric(
    name: str, totals: dict[str, object], fields: tuple[str, str]
) -> CoverageMetric:
    covered, total = (totals.get(field) for field in fields)
    if type(covered) is not int or type(total) is not int or not 0 <= covered <= total:
        raise ValueError(f"invalid {name.lower()} counts")
    return CoverageMetric(name, covered, total)


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--statements", type=coverage_floor, required=True)
    parser.add_argument("--branches", type=coverage_floor, required=True)
    args = parser.parse_args(arguments)
    try:
        metrics = read_metrics(args.report)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Invalid coverage report: {exc}", file=sys.stderr)
        return 2

    failed = False
    for metric, floor in zip(metrics, (args.statements, args.branches), strict=True):
        print(
            f"{metric.name}: {metric.percentage:.2f}% "
            f"({metric.covered}/{metric.total}), required {floor}%"
        )
        if not metric.meets(floor):
            print(f"{metric.name} coverage is below its threshold.", file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

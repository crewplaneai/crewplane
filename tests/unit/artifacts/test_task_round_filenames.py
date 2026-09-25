from pathlib import Path

import pytest

from crewplane.adapters.invokers.mock_invoker.fixtures import fixture_candidates
from crewplane.architecture.contracts import InvocationContext
from crewplane.architecture.contracts.artifacts import (
    build_task_round_filename,
    parse_audit_round,
    parse_task_round,
)


@pytest.mark.parametrize("task_id", ["alpha_executor_0", "task_round", "task_round12"])
@pytest.mark.parametrize("round_num", [-2, 0, 1, 12])
def test_task_round_filename_agrees_with_parser_and_mock_priority(
    task_id: str, round_num: int
) -> None:
    filename = build_task_round_filename(task_id, round_num)
    assert filename == f"{task_id}_round{round_num}.md"
    assert parse_task_round(Path(filename).stem) == (task_id, round_num)
    candidates = fixture_candidates(
        Path("fixtures"),
        InvocationContext(
            node_id="node.a",
            provider="alpha",
            task_id=task_id,
            role="executor",
            round_num=round_num,
            audit_round_num=2,
        ),
    )
    assert candidates[:4] == (
        Path(f"fixtures/node.a/review-audit-round-2/{filename}"),
        Path(f"fixtures/node.a/review-audit-round-2/executor-round-{round_num}.md"),
        Path(f"fixtures/node.a/review-audit-round-2/{task_id}.md"),
        Path("fixtures/node.a/review-audit-round-2/executor.md"),
    )
    assert candidates[5] == Path(f"fixtures/node.a/{filename}")
    assert candidates[-1] == Path("fixtures/default.md")


@pytest.mark.parametrize(
    "suffix, expected",
    [("+2", 2), ("0", 0), ("-2", -2), (" 3 ", 3), ("1_2", 12)],
)
def test_round_parsers_preserve_python_integer_suffixes(
    suffix: str, expected: int
) -> None:
    assert parse_task_round(f"task_round4_round{suffix}") == ("task_round4", expected)
    assert parse_audit_round(f"review-audit-round-{suffix}") == expected


@pytest.mark.parametrize("suffix", ["", "draft", "2_round3"])
def test_audit_parser_preserves_malformed_suffix_fallback(suffix: str) -> None:
    assert parse_audit_round(f"review-audit-round-{suffix}") == 0

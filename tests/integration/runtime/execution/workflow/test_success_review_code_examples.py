from __future__ import annotations

from textwrap import indent

import pytest

from crewplane.runtime.execution import check_consensus
from crewplane.runtime.execution.consensus import (
    evaluate_review_output,
    extract_verdict,
)
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    review_output,
)


@pytest.mark.parametrize("block_style", ["fenced", "indented"])
@pytest.mark.parametrize("position", ["before", "after"])
@pytest.mark.parametrize("verdict", ["NO_FINDINGS", "NITS_ONLY"])
def test_successful_review_ignores_verdict_examples_in_code_blocks(
    block_style: str,
    position: str,
    verdict: str,
) -> None:
    example = review_output(
        major="- Example finding, not an actual review issue.",
        verdict="CHANGES_REQUESTED",
    )
    block = (
        f"```markdown\n{example}\n```"
        if block_style == "fenced"
        else indent(example, "    ")
    )
    nitpicks = "- Tighten the section title." if verdict == "NITS_ONLY" else "None"
    actual = review_output(nitpicks=nitpicks, verdict=verdict)
    output = "\n\n".join((block, actual) if position == "before" else (actual, block))

    result = evaluate_review_output(output)

    assert result.approved
    assert result.verdict == verdict
    assert result.major_issues == "None"
    assert result.minor_issues == "None"
    assert result.nitpicks == nitpicks
    assert result.unresolved_issue_count == 0
    assert result.had_leading_text is (position == "before")
    assert result.had_trailing_text is (position == "after")
    assert extract_verdict(output) == verdict
    assert check_consensus([result])

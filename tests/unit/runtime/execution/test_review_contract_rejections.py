from __future__ import annotations

import pytest

from crewplane.runtime.execution.reviews.structured import (
    parse_review_result,
    review_output_uses_structured_contract,
)
from crewplane.runtime.execution.reviews.types import ReviewContractError


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (
            "## Major Issues\nNone.\n## Major Issues\nNone.\n---\nVERDICT: NO_FINDINGS",
            "duplicate structured review sections",
        ),
        (
            "## Nitpicks\nNone.\n## Major Issues\nNone.\n---\nVERDICT: NO_FINDINGS",
            "sections are out of order",
        ),
        ("## Major Issues\nNone.\nVERDICT: NO_FINDINGS", "review delimiter"),
        (
            "## Major Issues\nDefect.\n---\nVERDICT: CHANGES_REQUESTED",
            "omitted required section",
        ),
        (
            "## Major Issues\nNone.\n## Minor Issues\nNone.\n---\nVERDICT: NITS_ONLY",
            "omitted required section",
        ),
        ("## Major Issues\n---\nVERDICT: NO_FINDINGS", "must contain content"),
        (
            "## Major Issues\nDefect\n---\nFurther detail\n---\nVERDICT: NO_FINDINGS",
            "unexpected review delimiter",
        ),
        (
            "## Major Issues\nVERDICT: maybe\n---\nVERDICT: NO_FINDINGS",
            "unexpected verdict line",
        ),
        (
            "## Major Issues\n    ## Minor Issues\n---\nVERDICT: NO_FINDINGS",
            "unexpected section heading",
        ),
    ],
)
def test_review_parser_rejects_ambiguous_or_incomplete_contract(
    output: str, message: str
) -> None:
    assert review_output_uses_structured_contract(output)
    with pytest.raises(ReviewContractError, match=message):
        parse_review_result(output)


def test_explicit_changes_requested_is_preserved_when_all_sections_are_empty() -> None:
    output = "## Major Issues\nNone.\n## Minor Issues\nNone.\n## Nitpicks\nNone.\n---\n\nVERDICT: CHANGES_REQUESTED"
    result = parse_review_result(output)
    assert result.verdict == "CHANGES_REQUESTED"
    assert result.major_issues == result.minor_issues == result.nitpicks == "None"

from __future__ import annotations

import pytest

from crewplane.core.review_contract import (
    REVIEW_RESPONSE_INSTRUCTION,
    ParsedReviewResult,
    render_no_findings_review_contract,
    render_review_contract,
)
from crewplane.runtime.execution.reviews.markdown import review_section_for_heading
from crewplane.runtime.execution.reviews.structured import (
    extract_structured_review,
    is_review_heading,
    parse_review_result,
    review_output_uses_structured_contract,
    structured_review_warnings,
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


def test_review_contract_instruction_and_rendered_bytes() -> None:
    assert REVIEW_RESPONSE_INSTRUCTION == (
        "Return this review block at the end of your response:\n"
        "## Major Issues\nNone\n\n"
        "## Minor Issues\nNone\n\n"
        "## Nitpicks\nNone\n\n"
        "---\nVERDICT: CHANGES_REQUESTED | NITS_ONLY | NO_FINDINGS\n\n"
        "If you add optional commentary, put it above the review block and keep the "
        "review block last."
    )
    assert render_no_findings_review_contract() == (
        "## Major Issues\nNone\n\n## Minor Issues\nNone\n\n"
        "## Nitpicks\nNone\n\n---\nVERDICT: NO_FINDINGS\n"
    )
    assert render_review_contract(
        ParsedReviewResult("CHANGES_REQUESTED", "- Major\n  detail", "- Minor", "- Nit")
    ) == (
        "## Major Issues\n- Major\n  detail\n\n"
        "## Minor Issues\n- Minor\n\n## Nitpicks\n- Nit\n\n"
        "---\nVERDICT: CHANGES_REQUESTED\n"
    )


@pytest.mark.parametrize(
    ("heading", "section", "embedded"),
    [
        ("  mAjOr Issues  ", "major_issues", True),
        ("Minor Issues", "minor_issues", True),
        ("NITPICKS", "nitpicks", True),
        ("Major   Issues", None, True),
        ("Major-Issues", None, True),
        ("# Major Issues", None, True),
        ("Other", None, False),
    ],
)
def test_review_heading_recognition_preserves_distinct_normalization(
    heading, section, embedded
) -> None:
    assert review_section_for_heading(heading) == section
    assert is_review_heading(f"## {heading}") is embedded


def test_review_repair_warning_order_and_case_whitespace() -> None:
    output = "##   nItPiCkS  \n- Rename it.\n\n---\nverdict: no_findings\n\nAfterword."
    match = extract_structured_review(output)
    assert match is not None
    result = parse_review_result(output)
    assert (result.major_issues, result.minor_issues, result.nitpicks) == (
        "None",
        "None",
        "- Rename it.",
    )
    assert structured_review_warnings(match, result) == (
        "Repaired reviewer output: missing Major Issues section treated as None.",
        "Repaired reviewer output: missing Minor Issues section treated as None.",
        "Ignored commentary below the structured review block during review parsing.",
        "Normalized reviewer verdict from NO_FINDINGS to NITS_ONLY based on section content.",
    )

from __future__ import annotations

from dataclasses import dataclass

VERDICT_CHANGES_REQUESTED = "CHANGES_REQUESTED"
VERDICT_NITS_ONLY = "NITS_ONLY"
VERDICT_NO_FINDINGS = "NO_FINDINGS"
VALID_REVIEW_VERDICTS = frozenset(
    {VERDICT_CHANGES_REQUESTED, VERDICT_NITS_ONLY, VERDICT_NO_FINDINGS}
)
REQUIRED_EMPTY_SENTINEL = "None"
REVIEW_SECTIONS = (
    ("major_issues", "Major Issues"),
    ("minor_issues", "Minor Issues"),
    ("nitpicks", "Nitpicks"),
)
REVIEW_RESPONSE_INSTRUCTION = (
    "Return this review block at the end of your response:\n"
    + "".join(
        f"## {display_name}\n{REQUIRED_EMPTY_SENTINEL}\n\n"
        for _, display_name in REVIEW_SECTIONS
    )
    + "---\n"
    "VERDICT: CHANGES_REQUESTED | NITS_ONLY | NO_FINDINGS\n\n"
    "If you add optional commentary, put it above the review block and keep the "
    "review block last."
)


@dataclass(frozen=True)
class ParsedReviewResult:
    verdict: str
    major_issues: str
    minor_issues: str
    nitpicks: str


def render_review_contract(result: ParsedReviewResult) -> str:
    sections = "".join(
        f"## {display_name}\n{getattr(result, field_name)}\n\n"
        for field_name, display_name in REVIEW_SECTIONS
    )
    return f"{sections}---\nVERDICT: {result.verdict}\n"


def render_no_findings_review_contract() -> str:
    return render_review_contract(
        ParsedReviewResult(
            verdict=VERDICT_NO_FINDINGS,
            major_issues=REQUIRED_EMPTY_SENTINEL,
            minor_issues=REQUIRED_EMPTY_SENTINEL,
            nitpicks=REQUIRED_EMPTY_SENTINEL,
        )
    )

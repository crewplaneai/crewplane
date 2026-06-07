from __future__ import annotations

from dataclasses import dataclass

VERDICT_CHANGES_REQUESTED = "CHANGES_REQUESTED"
VERDICT_NITS_ONLY = "NITS_ONLY"
VERDICT_NO_FINDINGS = "NO_FINDINGS"
VALID_REVIEW_VERDICTS = frozenset(
    {VERDICT_CHANGES_REQUESTED, VERDICT_NITS_ONLY, VERDICT_NO_FINDINGS}
)
REQUIRED_EMPTY_SENTINEL = "None"
REVIEW_RESPONSE_INSTRUCTION = (
    "Return this review block at the end of your response:\n"
    "## Major Issues\n"
    "None\n\n"
    "## Minor Issues\n"
    "None\n\n"
    "## Nitpicks\n"
    "None\n\n"
    "---\n"
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
    return "\n".join(
        [
            "## Major Issues",
            result.major_issues,
            "",
            "## Minor Issues",
            result.minor_issues,
            "",
            "## Nitpicks",
            result.nitpicks,
            "",
            "---",
            f"VERDICT: {result.verdict}",
            "",
        ]
    )


def render_no_findings_review_contract() -> str:
    return render_review_contract(
        ParsedReviewResult(
            verdict=VERDICT_NO_FINDINGS,
            major_issues=REQUIRED_EMPTY_SENTINEL,
            minor_issues=REQUIRED_EMPTY_SENTINEL,
            nitpicks=REQUIRED_EMPTY_SENTINEL,
        )
    )

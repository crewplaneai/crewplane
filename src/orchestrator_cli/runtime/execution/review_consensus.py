from __future__ import annotations

from orchestrator_cli.core.review_contract import (
    REQUIRED_EMPTY_SENTINEL,
    VERDICT_CHANGES_REQUESTED,
    VERDICT_NITS_ONLY,
    VERDICT_NO_FINDINGS,
    ParsedReviewResult,
    render_review_contract,
)

from .plain_language_review import infer_plain_language_review
from .review_fingerprints import build_unresolved_fingerprints, count_unresolved_issues
from .review_types import EvaluatedReviewResult, ReviewContractError
from .structured_review import (
    extract_structured_review,
    normalize_review_result,
    parse_review_result,
    structured_review_warnings,
)

APPROVED_VERDICTS = frozenset({VERDICT_NITS_ONLY, VERDICT_NO_FINDINGS})


def is_approved_verdict(verdict: str) -> bool:
    return verdict in APPROVED_VERDICTS


def evaluate_review_output(output: str) -> EvaluatedReviewResult:
    structured_match = extract_structured_review(output)
    if structured_match is not None:
        try:
            parsed = normalize_review_result(
                ParsedReviewResult(
                    verdict=structured_match.verdict,
                    major_issues=structured_match.major_issues,
                    minor_issues=structured_match.minor_issues,
                    nitpicks=structured_match.nitpicks,
                )
            )
        except ReviewContractError as exc:
            return build_evaluated_review_result(
                parsed=ParsedReviewResult(
                    verdict=VERDICT_CHANGES_REQUESTED,
                    major_issues=REQUIRED_EMPTY_SENTINEL,
                    minor_issues=(
                        "Reviewer output included a malformed structured review block "
                        f"({exc}). Inspect the raw reviewer sidecar artifact."
                    ),
                    nitpicks=REQUIRED_EMPTY_SENTINEL,
                ),
                raw_text=output,
                evaluation_kind="unstructured_nonapproval",
                warnings=(
                    "Reviewer output included a structured review block, but it could "
                    "not be normalized and was treated as non-approval.",
                ),
                original_verdict=structured_match.verdict,
                had_leading_text=bool(structured_match.prefix.strip()),
                had_trailing_text=bool(structured_match.suffix.strip()),
            )
        warnings = structured_review_warnings(structured_match, parsed)
        return build_evaluated_review_result(
            parsed=parsed,
            raw_text=output,
            evaluation_kind="structured",
            warnings=warnings,
            original_verdict=structured_match.verdict,
            had_leading_text=bool(structured_match.prefix.strip()),
            had_trailing_text=bool(structured_match.suffix.strip()),
        )

    inferred = infer_plain_language_review(output)
    if inferred is not None:
        return build_evaluated_review_result(
            parsed=inferred.parsed,
            raw_text=output,
            evaluation_kind=inferred.evaluation_kind,
            warnings=inferred.warnings,
        )

    return build_evaluated_review_result(
        parsed=ParsedReviewResult(
            verdict=VERDICT_CHANGES_REQUESTED,
            major_issues=REQUIRED_EMPTY_SENTINEL,
            minor_issues=(
                "Reviewer output could not be normalized into the structured "
                "review contract, and approval could not be inferred. Inspect "
                "the raw reviewer sidecar artifact."
            ),
            nitpicks=REQUIRED_EMPTY_SENTINEL,
        ),
        raw_text=output,
        evaluation_kind="unstructured_nonapproval",
        warnings=(
            "Reviewer output did not include a structured review block, and "
            "approval could not be inferred from plain-language cues.",
        ),
    )


def build_evaluated_review_result(
    parsed: ParsedReviewResult,
    raw_text: str,
    evaluation_kind: str,
    warnings: tuple[str, ...],
    original_verdict: str | None = None,
    had_leading_text: bool = False,
    had_trailing_text: bool = False,
) -> EvaluatedReviewResult:
    return EvaluatedReviewResult(
        verdict=parsed.verdict,
        approved=is_approved_verdict(parsed.verdict),
        major_issues=parsed.major_issues,
        minor_issues=parsed.minor_issues,
        nitpicks=parsed.nitpicks,
        unresolved_fingerprints=build_unresolved_fingerprints(parsed),
        unresolved_issue_count=count_unresolved_issues(parsed),
        normalized_markdown=render_review_contract(parsed),
        raw_text=raw_text,
        evaluation_kind=evaluation_kind,
        warnings=warnings,
        original_verdict=original_verdict,
        had_leading_text=had_leading_text,
        had_trailing_text=had_trailing_text,
    )


def extract_verdict(output: str) -> str | None:
    try:
        return parse_review_result(output).verdict
    except ReviewContractError:
        return None


def ensure_evaluated_review_result(
    review_output: EvaluatedReviewResult | str,
) -> EvaluatedReviewResult:
    if isinstance(review_output, EvaluatedReviewResult):
        return review_output
    return evaluate_review_output(review_output)


def check_consensus(
    reviewer_outputs: list[EvaluatedReviewResult | str],
) -> bool:
    if not reviewer_outputs:
        return False
    results = [ensure_evaluated_review_result(output) for output in reviewer_outputs]
    return all(result.approved for result in results)

from __future__ import annotations

from orchestrator_cli.core.review_contract import (
    REQUIRED_EMPTY_SENTINEL,
    VERDICT_NITS_ONLY,
    VERDICT_NO_FINDINGS,
    ParsedReviewResult,
    render_review_contract,
)

from .plain_language_review import infer_plain_language_review
from .review_fingerprints import build_unresolved_fingerprints, count_unresolved_issues
from .review_types import EvaluatedReviewResult, ReviewContractError
from .structured_review import (
    extract_final_review_verdict,
    extract_structured_review,
    normalize_review_result,
    parse_review_result,
    review_output_uses_structured_contract,
    structured_review_warnings,
)

APPROVED_VERDICTS = frozenset({VERDICT_NITS_ONLY, VERDICT_NO_FINDINGS})


def is_approved_verdict(verdict: str | None) -> bool:
    return verdict in APPROVED_VERDICTS


def evaluate_review_output(output: str) -> EvaluatedReviewResult:
    try:
        structured_match = extract_structured_review(output)
    except ReviewContractError as exc:
        return build_unstructured_review_feedback_result(
            raw_text=output,
            warning=(
                "Reviewer output looked like a structured review, but it could "
                "not be parsed safely. Preserving it as unstructured reviewer "
                f"feedback: {exc}"
            ),
            original_verdict=extract_final_review_verdict(output),
        )

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
            return build_unstructured_review_feedback_result(
                raw_text=output,
                warning=(
                    "Reviewer output included a structured review block, but it could "
                    "not be normalized. Preserving it as unstructured reviewer "
                    f"feedback: {exc}"
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

    if review_output_uses_structured_contract(output):
        return build_unstructured_review_feedback_result(
            raw_text=output,
            warning=(
                "Reviewer output contained structured review markers, but no safe "
                "structured review result could be extracted. Preserving it as "
                "unstructured reviewer feedback."
            ),
            original_verdict=extract_final_review_verdict(output),
        )

    inferred = infer_plain_language_review(output)
    if inferred is not None:
        return build_evaluated_review_result(
            parsed=inferred.parsed,
            raw_text=output,
            evaluation_kind=inferred.evaluation_kind,
            warnings=inferred.warnings,
        )

    return build_unstructured_review_feedback_result(
        raw_text=output,
        warning=(
            "Reviewer output did not include a structured review block, and "
            "approval could not be inferred from plain-language cues. Preserving "
            "it as unstructured reviewer feedback."
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


def build_unstructured_review_feedback_result(
    raw_text: str,
    warning: str,
    original_verdict: str | None = None,
    had_leading_text: bool = False,
    had_trailing_text: bool = False,
) -> EvaluatedReviewResult:
    feedback = raw_text.strip()
    return EvaluatedReviewResult(
        verdict=None,
        approved=False,
        major_issues=REQUIRED_EMPTY_SENTINEL,
        minor_issues=REQUIRED_EMPTY_SENTINEL,
        nitpicks=REQUIRED_EMPTY_SENTINEL,
        unresolved_fingerprints=(),
        unresolved_issue_count=0,
        normalized_markdown=render_unstructured_review_feedback(feedback),
        raw_text=raw_text,
        evaluation_kind="unstructured_feedback",
        warnings=(warning,),
        original_verdict=original_verdict,
        had_leading_text=had_leading_text,
        had_trailing_text=had_trailing_text,
        unstructured_feedback=feedback,
    )


def render_unstructured_review_feedback(raw_text: str) -> str:
    feedback = raw_text or "No reviewer text was available."
    return "\n".join(
        [
            "# Unstructured Reviewer Feedback",
            "",
            (
                "The runtime could not normalize this reviewer response into "
                "Major Issues, Minor Issues, or Nitpicks. Treat the content "
                "below as raw reviewer feedback, not as normalized candidate "
                "findings."
            ),
            "",
            "## Raw Feedback",
            "",
            feedback,
            "",
        ]
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

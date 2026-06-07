from __future__ import annotations

from dataclasses import dataclass

from orchestrator_cli.core.review_contract import (
    REQUIRED_EMPTY_SENTINEL,
    VERDICT_CHANGES_REQUESTED,
    VERDICT_NITS_ONLY,
    VERDICT_NO_FINDINGS,
    ParsedReviewResult,
)

from .structured_review import normalize_text_token

_APPROVAL_CUES = (
    "approved",
    "good to go",
    "lgtm",
    "looks good",
    "looks great",
    "looks solid",
    "merge ready",
    "no blockers",
    "no blocking issues",
    "ready to merge",
    "ship it",
)
_BLOCKER_CUES = (
    "blocker remains",
    "blockers remain",
    "can not approve",
    "cannot approve",
    "changes requested",
    "do not approve",
    "issue remains",
    "issues remain",
    "must fix",
    "needs changes",
    "not good to go",
    "not lgtm",
    "not approved",
    "not ready",
    "not ready to merge",
    "remaining blocker",
    "remaining blockers",
    "remaining issue",
    "remaining issues",
    "should fix",
)
_NITPICK_CUES = (
    "nit",
    "nitpick",
    "nitpicks",
    "nit only",
    "non blocking",
    "optional",
    "polish",
    "style only",
)


@dataclass(frozen=True)
class PlainLanguageReviewInference:
    parsed: ParsedReviewResult
    evaluation_kind: str
    warnings: tuple[str, ...]


def infer_plain_language_review(output: str) -> PlainLanguageReviewInference | None:
    stripped_output = output.strip()
    if not stripped_output:
        return None

    normalized_output = normalize_text_token(stripped_output)
    has_blocker = any(contains_cue(normalized_output, cue) for cue in _BLOCKER_CUES)
    has_approval = any(contains_cue(normalized_output, cue) for cue in _APPROVAL_CUES)
    has_nitpicks = any(contains_cue(normalized_output, cue) for cue in _NITPICK_CUES)

    if has_approval and not has_blocker:
        if has_nitpicks:
            parsed = ParsedReviewResult(
                verdict=VERDICT_NITS_ONLY,
                major_issues=REQUIRED_EMPTY_SENTINEL,
                minor_issues=REQUIRED_EMPTY_SENTINEL,
                nitpicks=stripped_output,
            )
            warning = "Inferred NITS_ONLY approval from plain-language reviewer output."
        else:
            parsed = ParsedReviewResult(
                verdict=VERDICT_NO_FINDINGS,
                major_issues=REQUIRED_EMPTY_SENTINEL,
                minor_issues=REQUIRED_EMPTY_SENTINEL,
                nitpicks=REQUIRED_EMPTY_SENTINEL,
            )
            warning = (
                "Inferred NO_FINDINGS approval from plain-language reviewer output."
            )
        return PlainLanguageReviewInference(
            parsed=parsed,
            evaluation_kind="plain_language_approval",
            warnings=(warning,),
        )

    if has_blocker:
        parsed = ParsedReviewResult(
            verdict=VERDICT_CHANGES_REQUESTED,
            major_issues=REQUIRED_EMPTY_SENTINEL,
            minor_issues=stripped_output,
            nitpicks=REQUIRED_EMPTY_SENTINEL,
        )
        return PlainLanguageReviewInference(
            parsed=parsed,
            evaluation_kind="plain_language_changes_requested",
            warnings=(
                "Inferred CHANGES_REQUESTED from blocker language in unstructured "
                "reviewer output.",
            ),
        )

    return None


def contains_cue(normalized_output: str, cue: str) -> bool:
    return f" {cue} " in f" {normalized_output} "

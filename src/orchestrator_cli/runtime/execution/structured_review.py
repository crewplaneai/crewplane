from __future__ import annotations

import re

from orchestrator_cli.core.review_contract import (
    REQUIRED_EMPTY_SENTINEL,
    VALID_REVIEW_VERDICTS,
    VERDICT_CHANGES_REQUESTED,
    VERDICT_NITS_ONLY,
    VERDICT_NO_FINDINGS,
    ParsedReviewResult,
)

from .review_types import ReviewContractError, StructuredReviewMatch

_VERDICT_PATTERN = "|".join(sorted(VALID_REVIEW_VERDICTS))
_STRUCTURED_REVIEW_PATTERN = re.compile(
    rf"(?is)##\s*Major\s+Issues\s*(?P<major>.*?)\s*"
    rf"##\s*Minor\s+Issues\s*(?P<minor>.*?)\s*"
    rf"##\s*Nitpicks\s*(?P<nitpicks>.*?)\s*"
    rf"---\s*VERDICT:\s*(?P<verdict>{_VERDICT_PATTERN})\b"
)
_GENERIC_EMPTY_SENTINELS = frozenset(
    {
        "",
        "n a",
        "na",
        "no comment",
        "no comments",
        "no finding",
        "no findings",
        "no issue",
        "no issues",
        "none",
        "none found",
        "none noted",
        "not applicable",
        "nothing",
        "nothing to report",
    }
)


def extract_structured_review(output: str) -> StructuredReviewMatch | None:
    matches = list(_STRUCTURED_REVIEW_PATTERN.finditer(output))
    if not matches:
        return None

    match = matches[-1]
    return StructuredReviewMatch(
        verdict=match.group("verdict").strip().upper(),
        major_issues=match.group("major"),
        minor_issues=match.group("minor"),
        nitpicks=match.group("nitpicks"),
        prefix=output[: match.start()],
        suffix=output[match.end() :],
    )


def normalize_review_result(result: ParsedReviewResult) -> ParsedReviewResult:
    major_issues = normalize_review_section("Major Issues", result.major_issues)
    minor_issues = normalize_review_section("Minor Issues", result.minor_issues)
    nitpicks = normalize_review_section("Nitpicks", result.nitpicks)
    normalized = ParsedReviewResult(
        verdict=result.verdict.strip().upper(),
        major_issues=major_issues,
        minor_issues=minor_issues,
        nitpicks=nitpicks,
    )
    normalized_verdict = canonical_review_verdict(normalized)
    return ParsedReviewResult(
        verdict=normalized_verdict,
        major_issues=normalized.major_issues,
        minor_issues=normalized.minor_issues,
        nitpicks=normalized.nitpicks,
    )


def parse_review_result(output: str) -> ParsedReviewResult:
    match = extract_structured_review(output)
    if match is None:
        raise ReviewContractError(
            "Reviewer output must include a structured review block."
        )
    return normalize_review_result(
        ParsedReviewResult(
            verdict=match.verdict,
            major_issues=match.major_issues,
            minor_issues=match.minor_issues,
            nitpicks=match.nitpicks,
        )
    )


def normalize_review_section(name: str, content: str) -> str:
    normalized = content.strip()
    if not normalized:
        raise ReviewContractError(
            f"Reviewer section '{name}' must contain content or '{REQUIRED_EMPTY_SENTINEL}'."
        )
    if is_empty_section(normalized, name):
        return REQUIRED_EMPTY_SENTINEL
    validate_review_section_content(name, normalized)
    return normalized


def is_empty_section(content: str, name: str | None = None) -> bool:
    normalized = normalize_text_token(content)
    if normalized in _GENERIC_EMPTY_SENTINELS:
        return True
    if name is None:
        return False
    section_name = normalize_text_token(name)
    return normalized in {
        f"no {section_name}",
        f"none for {section_name}",
        f"nothing for {section_name}",
    }


def validate_review_section_content(name: str, content: str) -> None:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "---":
            raise ReviewContractError(
                f"Reviewer section '{name}' contains an unexpected review delimiter."
            )
        if stripped.upper().startswith("VERDICT:"):
            raise ReviewContractError(
                f"Reviewer section '{name}' contains an unexpected verdict line."
            )
        if is_review_heading(stripped):
            raise ReviewContractError(
                f"Reviewer section '{name}' contains an unexpected section heading."
            )


def is_review_heading(line: str) -> bool:
    if not line.startswith("##"):
        return False
    return normalize_text_token(line[2:]) in {
        "major issues",
        "minor issues",
        "nitpicks",
    }


def canonical_review_verdict(result: ParsedReviewResult) -> str:
    has_major = not is_empty_section(result.major_issues)
    has_minor = not is_empty_section(result.minor_issues)
    has_nitpicks = not is_empty_section(result.nitpicks)

    if has_major or has_minor:
        return VERDICT_CHANGES_REQUESTED
    if has_nitpicks:
        return VERDICT_NITS_ONLY
    if result.verdict == VERDICT_CHANGES_REQUESTED:
        return VERDICT_CHANGES_REQUESTED
    return VERDICT_NO_FINDINGS


def structured_review_warnings(
    match: StructuredReviewMatch,
    normalized: ParsedReviewResult,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if match.suffix.strip():
        warnings.append(
            "Ignored commentary below the structured review block during review parsing."
        )
    if match.verdict != normalized.verdict:
        warnings.append(
            "Normalized reviewer verdict from "
            f"{match.verdict} to {normalized.verdict} based on section content."
        )
    return tuple(warnings)


def normalize_text_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

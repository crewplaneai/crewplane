from __future__ import annotations

import re

from crewplane.core.review_contract import (
    REQUIRED_EMPTY_SENTINEL,
    VALID_REVIEW_VERDICTS,
    VERDICT_CHANGES_REQUESTED,
    VERDICT_NITS_ONLY,
    VERDICT_NO_FINDINGS,
    ParsedReviewResult,
)

from .markdown import (
    ReviewHeading,
    code_block_lines,
    collect_root_h2_review_headings,
)
from .types import ReviewContractError, StructuredReviewMatch

_VERDICT_PATTERN = "|".join(sorted(VALID_REVIEW_VERDICTS))
_VERDICT_LINE_PATTERN = re.compile(
    rf"^\s*VERDICT:\s*(?P<verdict>{_VERDICT_PATTERN})\s*$",
    re.IGNORECASE,
)
_SECTION_ORDER = ("major_issues", "minor_issues", "nitpicks")
_SECTION_INDEX = {section: index for index, section in enumerate(_SECTION_ORDER)}
_SECTION_DISPLAY_NAME = {
    "major_issues": "Major Issues",
    "minor_issues": "Minor Issues",
    "nitpicks": "Nitpicks",
}
_APPROVED_REPAIRABLE_MISSING_SECTIONS = {
    VERDICT_NITS_ONLY: frozenset({"major_issues", "minor_issues"}),
    VERDICT_NO_FINDINGS: frozenset(_SECTION_ORDER),
}
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
    lines = output.splitlines(keepends=True)
    verdict = extract_final_review_verdict(output)
    if verdict is None:
        return None

    verdict_line = final_review_verdict_line(output)
    if verdict_line is None:
        return None

    headings = collect_root_h2_review_headings(output, verdict_line)
    review_headings = final_review_heading_run(headings)
    if not review_headings:
        return None

    validate_review_heading_run(review_headings)
    content_end_line = review_content_end_line(lines, verdict_line)
    sections = collect_review_section_content(lines, review_headings, content_end_line)
    earlier_sections = collect_earlier_review_sections(headings, review_headings)
    repaired_sections, warnings = repair_missing_sections(
        verdict,
        sections,
        earlier_sections,
    )

    return StructuredReviewMatch(
        verdict=verdict,
        major_issues=repaired_sections["major_issues"],
        minor_issues=repaired_sections["minor_issues"],
        nitpicks=repaired_sections["nitpicks"],
        prefix="".join(lines[: review_headings[0].start_line]),
        suffix="".join(lines[verdict_line + 1 :]),
        warnings=warnings,
    )


def extract_final_review_verdict(output: str) -> str | None:
    verdict_line = final_review_verdict_line(output)
    if verdict_line is None:
        return None
    match = _VERDICT_LINE_PATTERN.match(output.splitlines()[verdict_line])
    if match is None:
        return None
    return match.group("verdict").upper()


def final_review_verdict_line(output: str) -> int | None:
    ignored_lines = code_block_lines(output)
    verdict_line: int | None = None
    for index, line in enumerate(output.splitlines()):
        if index in ignored_lines:
            continue
        if _VERDICT_LINE_PATTERN.match(line) is not None:
            verdict_line = index
    return verdict_line


def review_output_uses_structured_contract(output: str) -> bool:
    if extract_final_review_verdict(output) is not None:
        return True
    return any(
        heading.section is not None
        for heading in collect_root_h2_review_headings(output)
    )


def final_review_heading_run(headings: list[ReviewHeading]) -> list[ReviewHeading]:
    if not headings or headings[-1].section is None:
        return []
    start_index = len(headings) - 1
    while start_index > 0 and headings[start_index - 1].section is not None:
        start_index -= 1
    return headings[start_index:]


def validate_review_heading_run(headings: list[ReviewHeading]) -> None:
    sections = [heading.section for heading in headings if heading.section is not None]
    if len(set(sections)) != len(sections):
        raise ReviewContractError(
            "Reviewer output contains duplicate structured review sections."
        )
    ordered_sections = sorted(sections, key=_SECTION_INDEX.__getitem__)
    if sections != ordered_sections:
        raise ReviewContractError(
            "Reviewer output structured review sections are out of order."
        )


def review_content_end_line(lines: list[str], verdict_line: int) -> int:
    cursor = verdict_line
    while cursor > 0 and not lines[cursor - 1].strip():
        cursor -= 1
    if cursor == 0 or lines[cursor - 1].strip() != "---":
        raise ReviewContractError(
            "Reviewer output must include a review delimiter before the final verdict."
        )
    return cursor - 1


def collect_review_section_content(
    lines: list[str],
    headings: list[ReviewHeading],
    content_end_line: int,
) -> dict[str, str]:
    sections: dict[str, str] = {}
    for index, heading in enumerate(headings):
        if heading.section is None:
            continue
        next_start = (
            headings[index + 1].start_line
            if index + 1 < len(headings)
            else content_end_line
        )
        sections[heading.section] = "".join(
            lines[heading.content_start_line : next_start]
        )
    return sections


def collect_earlier_review_sections(
    headings: list[ReviewHeading],
    review_headings: list[ReviewHeading],
) -> frozenset[str]:
    run_start_line = review_headings[0].start_line
    return frozenset(
        heading.section
        for heading in headings
        if heading.section is not None and heading.start_line < run_start_line
    )


def repair_missing_sections(
    verdict: str,
    sections: dict[str, str],
    earlier_sections: frozenset[str],
) -> tuple[dict[str, str], tuple[str, ...]]:
    missing_sections = set(_SECTION_ORDER).difference(sections)
    if not missing_sections:
        return sections, ()

    ambiguous_sections = missing_sections.intersection(earlier_sections)
    if ambiguous_sections:
        missing_names = ", ".join(
            _SECTION_DISPLAY_NAME[section]
            for section in sorted(ambiguous_sections, key=_SECTION_INDEX.__getitem__)
        )
        raise ReviewContractError(
            "Reviewer output omitted section(s) from the final review block after "
            f"using the same section heading earlier: {missing_names}."
        )

    repairable_sections = _APPROVED_REPAIRABLE_MISSING_SECTIONS.get(
        verdict, frozenset()
    )
    unrepaired_sections = missing_sections.difference(repairable_sections)
    if unrepaired_sections:
        missing_names = ", ".join(
            _SECTION_DISPLAY_NAME[section]
            for section in sorted(unrepaired_sections, key=_SECTION_INDEX.__getitem__)
        )
        raise ReviewContractError(
            f"Reviewer output omitted required section(s): {missing_names}."
        )

    repaired = dict(sections)
    warnings: list[str] = []
    for section in sorted(missing_sections, key=_SECTION_INDEX.__getitem__):
        repaired[section] = REQUIRED_EMPTY_SENTINEL
        warnings.append(
            "Repaired reviewer output: missing "
            f"{_SECTION_DISPLAY_NAME[section]} section treated as None."
        )
    return repaired, tuple(warnings)


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
    warnings: list[str] = list(match.warnings)
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

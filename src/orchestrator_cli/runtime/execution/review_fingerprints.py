from __future__ import annotations

import hashlib
import re

from orchestrator_cli.core.review_contract import ParsedReviewResult

from .structured_review import is_empty_section, normalize_text_token

_ISSUE_ITEM_MARKER_PATTERN = re.compile(r"^\s*(?:[-*+]\s+|\d+\.\s+)")
_PATH_REFERENCE_PATTERN = re.compile(
    r"(?P<path>[A-Za-z0-9_./-]+\.[A-Za-z0-9_]+)(?::\d+(?:-\d+)?)?"
)
_ISSUE_KEYWORD_PATTERN = re.compile(r"[a-z0-9]+")
MAX_REFERENCE_FINGERPRINT_KEYWORDS = 3
_REFERENCE_FINGERPRINT_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "are",
        "before",
        "change",
        "changes",
        "ensure",
        "executor",
        "file",
        "files",
        "fix",
        "for",
        "in",
        "issue",
        "issues",
        "line",
        "lines",
        "make",
        "missing",
        "need",
        "needed",
        "needs",
        "on",
        "only",
        "or",
        "path",
        "review",
        "reviewer",
        "still",
        "that",
        "the",
        "these",
        "this",
        "those",
        "to",
        "update",
        "use",
        "used",
        "using",
        "with",
        "without",
    }
)


def build_unresolved_fingerprints(result: ParsedReviewResult) -> tuple[str, ...]:
    fingerprints: set[str] = set()
    for severity, content in (
        ("major", result.major_issues),
        ("minor", result.minor_issues),
    ):
        if is_empty_section(content):
            continue
        for issue in _split_review_issues(content):
            fingerprints.add(_fingerprint_review_section(severity, issue))
            fingerprints.update(_build_reference_fingerprints(severity, issue))
    return tuple(sorted(fingerprints))


def count_unresolved_issues(result: ParsedReviewResult) -> int:
    issue_count = 0
    for content in (result.major_issues, result.minor_issues):
        if is_empty_section(content):
            continue
        issue_count += len(_split_review_issues(content))
    return issue_count


def _fingerprint_review_section(severity: str, content: str) -> str:
    normalized_content = normalize_text_token(content)
    return hashlib.sha256(f"{severity}:{normalized_content}".encode()).hexdigest()[:12]


def _build_reference_fingerprints(severity: str, issue: str) -> set[str]:
    references = _extract_issue_references(issue)
    keywords = _extract_issue_keywords(issue)[:MAX_REFERENCE_FINGERPRINT_KEYWORDS]
    return {
        hashlib.sha256(f"{severity}:ref:{reference}:{keyword}".encode()).hexdigest()[
            :12
        ]
        for reference in references
        for keyword in keywords
    }


def _extract_issue_references(issue: str) -> tuple[str, ...]:
    references = {
        match.group("path").lower() for match in _PATH_REFERENCE_PATTERN.finditer(issue)
    }
    return tuple(sorted(references))


def _extract_issue_keywords(issue: str) -> tuple[str, ...]:
    scrubbed_issue = _PATH_REFERENCE_PATTERN.sub(" ", issue.lower())
    keywords: list[str] = []
    seen: set[str] = set()
    for token in _ISSUE_KEYWORD_PATTERN.findall(scrubbed_issue):
        if len(token) < 4 or token in _REFERENCE_FINGERPRINT_STOPWORDS or token in seen:
            continue
        seen.add(token)
        keywords.append(token)
    return tuple(keywords)


def _split_review_issues(content: str) -> tuple[str, ...]:
    issues: list[str] = []
    current_issue: list[str] = []

    for raw_line in content.strip().splitlines():
        if _ISSUE_ITEM_MARKER_PATTERN.match(raw_line):
            _append_issue(issues, current_issue)
            current_issue = [_strip_issue_item_marker(raw_line)]
            continue
        if current_issue or raw_line.strip():
            current_issue.append(raw_line.strip())

    _append_issue(issues, current_issue)
    return tuple(issues) if issues else (content.strip(),)


def _append_issue(issues: list[str], current_issue: list[str]) -> None:
    issue = "\n".join(line for line in current_issue if line).strip()
    if issue:
        issues.append(issue)


def _strip_issue_item_marker(line: str) -> str:
    return _ISSUE_ITEM_MARKER_PATTERN.sub("", line, count=1).strip()

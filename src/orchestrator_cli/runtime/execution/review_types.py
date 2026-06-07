from __future__ import annotations

from dataclasses import dataclass


class ReviewContractError(RuntimeError):
    """Raised when a structured reviewer response cannot be extracted."""


@dataclass(frozen=True)
class StructuredReviewMatch:
    verdict: str
    major_issues: str
    minor_issues: str
    nitpicks: str
    prefix: str
    suffix: str


@dataclass(frozen=True)
class EvaluatedReviewResult:
    verdict: str
    approved: bool
    major_issues: str
    minor_issues: str
    nitpicks: str
    unresolved_fingerprints: tuple[str, ...]
    unresolved_issue_count: int
    normalized_markdown: str
    raw_text: str
    evaluation_kind: str
    warnings: tuple[str, ...]
    original_verdict: str | None = None
    had_leading_text: bool = False
    had_trailing_text: bool = False

    def to_metadata_dict(self) -> dict[str, object]:
        return {
            "approved": self.approved,
            "evaluation_kind": self.evaluation_kind,
            "had_leading_text": self.had_leading_text,
            "had_trailing_text": self.had_trailing_text,
            "normalized_verdict": self.verdict,
            "original_verdict": self.original_verdict,
            "unresolved_issue_count": self.unresolved_issue_count,
            "warnings": list(self.warnings),
        }

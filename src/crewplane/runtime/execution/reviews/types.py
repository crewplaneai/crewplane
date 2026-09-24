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
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvaluatedReviewResult:
    verdict: str | None
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
    unstructured_feedback: str | None = None

    def to_metadata_dict(self) -> dict[str, object]:
        return {**self._common_fields(), "normalized_verdict": self.verdict}

    def to_state_dict(self) -> dict[str, object]:
        return {
            **self._common_fields(),
            "verdict": self.verdict,
            "major_issues": self.major_issues,
            "minor_issues": self.minor_issues,
            "nitpicks": self.nitpicks,
            "unresolved_fingerprints": list(self.unresolved_fingerprints),
        }

    def _common_fields(self) -> dict[str, object]:
        return {
            "approved": self.approved,
            "evaluation_kind": self.evaluation_kind,
            "had_leading_text": self.had_leading_text,
            "had_trailing_text": self.had_trailing_text,
            "original_verdict": self.original_verdict,
            "unstructured_feedback": self.unstructured_feedback,
            "unresolved_issue_count": self.unresolved_issue_count,
            "warnings": list(self.warnings),
        }

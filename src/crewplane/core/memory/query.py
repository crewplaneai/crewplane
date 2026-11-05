from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import Field

from .models import FrozenModel

QUERY_REVISION = 1
MAX_QUERY_CHARS = 32_000
MAX_QUERY_TERMS = 256
STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "do",
        "for",
        "from",
        "has",
        "have",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "our",
        "should",
        "that",
        "the",
        "their",
        "then",
        "this",
        "to",
        "use",
        "was",
        "we",
        "were",
        "will",
        "with",
        "you",
        "your",
    ]
)
_TOKEN = re.compile(
    r"(?:(?:\.\.?[/\\])+|[/\\])?\.?[\w]+(?:[./:\\-]+[\w]+)*"
    r"|(?P<prefix_only>(?:\.\.?[/\\])+)",
    re.UNICODE,
)


class QueryContribution(FrozenModel):
    kind: Literal["literal", "identifier", "variable", "file", "input"]
    source: str
    text: str


class QueryExclusion(FrozenModel):
    source: str
    reason: str


class MemoryQuery(FrozenModel):
    policy_revision: int = Field(default=QUERY_REVISION, strict=True)
    terms: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()
    contributions: tuple[QueryContribution, ...] = ()
    exclusions: tuple[QueryExclusion, ...] = ()
    examined_chars: int = Field(default=0, strict=True, ge=0, le=MAX_QUERY_CHARS)


def normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def tokens(
    value: str, include_simple_identifiers: bool = False
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    terms: dict[str, None] = {}
    identifiers: dict[str, None] = {}
    for match in _TOKEN.finditer(unicodedata.normalize("NFKC", value)):
        # Consume no-tail runs once rather than retrying from every inner segment.
        if match.lastgroup == "prefix_only":
            continue
        original = match.group()
        whole = original.casefold()
        if whole not in STOP_WORDS:
            terms[whole] = None
        if include_simple_identifiers or re.search(r"[./:\\_\-]|[a-z][A-Z]", original):
            identifiers[whole] = None
        components = re.sub(r"([a-z\d])([A-Z])", r"\1 \2", original)
        for component in re.split(r"[\W_]+", components.casefold()):
            if component and component not in STOP_WORDS:
                terms[component] = None
    return tuple(terms), tuple(identifiers)


def build_query(
    contributions: tuple[QueryContribution, ...],
    exclusions: tuple[QueryExclusion, ...] = (),
) -> MemoryQuery:
    terms: dict[str, None] = {}
    identifiers: dict[str, None] = {}
    captured: list[QueryContribution] = []
    omitted = list(exclusions)
    examined = 0
    prioritized = sorted(contributions, key=lambda item: item.kind in {"file", "input"})
    for contribution in prioritized:
        remaining = MAX_QUERY_CHARS - examined
        if remaining == 0 or len(terms) == MAX_QUERY_TERMS:
            reason = "query character limit" if remaining == 0 else "query term limit"
            omitted.append(QueryExclusion(source=contribution.source, reason=reason))
            continue
        text = contribution.text[:remaining]
        examined += len(text)
        captured.append(contribution.model_copy(update={"text": text}))
        if len(text) < len(contribution.text):
            omitted.append(
                QueryExclusion(
                    source=contribution.source,
                    reason="query character limit; remaining text omitted",
                )
            )
        found, whole_identifiers = tokens(text, contribution.kind == "identifier")
        for term in found:
            if term not in terms and len(terms) == MAX_QUERY_TERMS:
                omitted.append(
                    QueryExclusion(
                        source=contribution.source,
                        reason="query term limit; remaining terms omitted",
                    )
                )
                break
            terms[term] = None
            if term in whole_identifiers:
                identifiers[term] = None
    return MemoryQuery(
        terms=tuple(terms),
        identifiers=tuple(identifiers),
        contributions=tuple(captured),
        exclusions=tuple(omitted),
        examined_chars=examined,
    )

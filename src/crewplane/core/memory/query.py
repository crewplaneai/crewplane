from __future__ import annotations

import re
import unicodedata
from collections import deque
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
    for contribution, limit in zip(
        prioritized, _character_limits(prioritized), strict=True
    ):
        if limit == 0 and contribution.text:
            omitted.append(
                QueryExclusion(
                    source=contribution.source, reason="query character limit"
                )
            )
            continue
        text = contribution.text[:limit]
        examined += len(text)
        captured.append(contribution.model_copy(update={"text": text}))
        if len(text) < len(contribution.text):
            omitted.append(
                QueryExclusion(
                    source=contribution.source,
                    reason="query character limit; remaining text omitted",
                )
            )
    tokenized = [tokens(item.text, item.kind == "identifier") for item in captured]
    admitted = _shared_terms(tuple(found for found, _ in tokenized))
    for contribution, (found, whole_identifiers) in zip(
        captured, tokenized, strict=True
    ):
        whole = set(whole_identifiers)
        if any(term not in admitted for term in found):
            omitted.append(
                QueryExclusion(
                    source=contribution.source,
                    reason="query term limit; remaining terms omitted",
                )
            )
        for term in found:
            if term in admitted:
                terms[term] = None
                if term in whole:
                    identifiers[term] = None
    return MemoryQuery(
        terms=tuple(terms),
        identifiers=tuple(identifiers),
        contributions=tuple(captured),
        exclusions=tuple(omitted),
        examined_chars=examined,
    )


def _character_limits(contributions: list[QueryContribution]) -> list[int]:
    lengths = [len(item.text) for item in contributions]
    if sum(lengths) <= MAX_QUERY_CHARS:
        return lengths
    # Find an equal prefix allowance, recycling capacity from shorter sources.
    low, high = 0, MAX_QUERY_CHARS + 1
    while low + 1 < high:
        middle = (low + high) // 2
        if sum(min(length, middle) for length in lengths) <= MAX_QUERY_CHARS:
            low = middle
        else:
            high = middle
    limits = [min(length, low) for length in lengths]
    remaining = MAX_QUERY_CHARS - sum(limits)
    for index, length in enumerate(lengths):
        if remaining and limits[index] < length:
            limits[index] += 1
            remaining -= 1
    return limits


def _shared_terms(contributions: tuple[tuple[str, ...], ...]) -> set[str]:
    admitted: set[str] = set()
    active = deque(iter(found) for found in contributions if found)
    # Each source admits one new term per turn; duplicates consume no capacity.
    while active and len(admitted) < MAX_QUERY_TERMS:
        candidates = active.popleft()
        for term in candidates:
            if term not in admitted:
                admitted.add(term)
                active.append(candidates)
                break
    return admitted

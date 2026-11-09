"""Rank accepted memory lexically and freeze bounded, whole-record recall.

Selection is deterministic and reads only the supplied snapshot and query; it
performs no I/O or mutation. Diagnostic receipts are retained separately from
the semantic payload used in execution identity.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from .models import FrozenModel, MemoryRevision, MemorySnapshot
from .query import (
    MAX_QUERY_TERMS,
    QUERY_REVISION,
    MemoryQuery,
    build_query,
    normalize,
    tokens,
)
from .rendering import RENDERING_REVISION, render_block

RETRIEVAL_REVISION = 1


class SelectedMemory(FrozenModel):
    """Freeze a record revision and its three-component lexical score.

    Score components count identifier/tag, title/tag term, and body term matches,
    in descending ranking priority. Eligibility is checked by MemorySelection.
    """

    record_id: str
    revision: MemoryRevision
    score: tuple[int, int, int]


class MemorySelection(FrozenModel):
    """Freeze one node role's query, ranked records, rendering, and omissions.

    Construction raises pydantic.ValidationError for inconsistent query, policy,
    limits, rendering, revision eligibility, scores, or ranking. Limits must be
    positive integers, and the role-stream append boundary must be nonnegative.
    """

    node_id: str
    role: Literal["executor", "reviewer"]
    query: MemoryQuery
    max_records: int = Field(strict=True, gt=0)
    max_chars: int = Field(strict=True, gt=0)
    append_after_fragment_count: int = Field(strict=True, ge=0)
    retrieval_revision: int = Field(default=RETRIEVAL_REVISION, strict=True)
    rendering_revision: int = Field(default=RENDERING_REVISION, strict=True)
    selected: tuple[SelectedMemory, ...] = ()
    rendered: str = ""
    rendered_chars: int = Field(default=0, strict=True, ge=0)
    omissions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_frozen_selection(self) -> Self:
        """Validate query/policy, packing, then revision scores and rank order.

        Raises:
            ValueError: A frozen recall contract fails, at the first failed check.
        """
        self._validate_query_and_policy()
        self._validate_packing()
        self._validate_selected_revisions()
        return self

    def _validate_query_and_policy(self) -> None:
        """Reconstruct eligible query inputs before checking policy and term bounds."""
        reconstructed = build_query(self.query.contributions)
        if (self.query.terms, self.query.identifiers, self.query.examined_chars) != (
            reconstructed.terms,
            reconstructed.identifiers,
            reconstructed.examined_chars,
        ):
            raise ValueError(
                "Memory query disagrees with its frozen eligible contributions."
            )
        if (
            self.query.policy_revision,
            self.retrieval_revision,
            self.rendering_revision,
        ) != (QUERY_REVISION, RETRIEVAL_REVISION, RENDERING_REVISION):
            raise ValueError("Unsupported memory retrieval/rendering policy revision.")
        if (
            len(self.query.terms) > MAX_QUERY_TERMS
            or len(set(self.query.terms)) != len(self.query.terms)
            or not set(self.query.identifiers) <= set(self.query.terms)
        ):
            raise ValueError("Invalid memory query term identities or bounds.")

    def _validate_packing(self) -> None:
        """Check uniqueness and limits before verifying complete rendered records."""
        if len({item.record_id for item in self.selected}) != len(self.selected):
            raise ValueError("A memory record can be selected only once per role.")
        if len(self.selected) > self.max_records or len(self.rendered) > self.max_chars:
            raise ValueError("Memory selection exceeds recall limits.")
        if self.rendered_chars != len(self.rendered) or self.rendered != render_block(
            tuple((item.record_id, item.revision) for item in self.selected)
        ):
            raise ValueError(
                "Memory selection rendering disagrees with complete frozen records."
            )

    def _validate_selected_revisions(self) -> None:
        """Require accepted lexical matches with exact scores, then ranked order."""
        for item in self.selected:
            if (
                item.revision.disposition != "accepted"
                or item.score != score_revision(self.query, item.revision)
                or not any(item.score)
            ):
                raise ValueError(
                    "Selected memory must be an accepted matching revision with its lexical score."
                )
        if tuple(sorted(self.selected, key=selection_order)) != self.selected:
            raise ValueError("Memory selection is not in deterministic rank order.")

    def semantic_payload(self) -> dict[str, object]:
        """Return recall inputs and selected context used in execution identity.

        Deliberately exclude query contributions, exclusions, examined-character
        diagnostics, omissions, scores, and the redundant rendered-character count.
        Selected revisions contribute IDs and content/evidence digests rather than
        full metadata, so creation, acceptance, observation, and capture timestamps
        do not affect identity. The complete rendered context remains included.
        """
        return {
            "node_id": self.node_id,
            "role": self.role,
            "max_records": self.max_records,
            "max_chars": self.max_chars,
            "append_after_fragment_count": self.append_after_fragment_count,
            "query": {
                "terms": self.query.terms,
                "identifiers": self.query.identifiers,
                "policy_revision": self.query.policy_revision,
            },
            "retrieval_revision": self.retrieval_revision,
            "rendering_revision": self.rendering_revision,
            "selected": [
                {
                    "record_id": item.record_id,
                    "revision_id": item.revision.revision_id,
                    "content_digest": item.revision.content_digest,
                    "evidence_digest": item.revision.evidence_digest,
                }
                for item in self.selected
            ],
            "rendered": self.rendered,
        }


def score_revision(
    query: MemoryQuery, revision: MemoryRevision
) -> tuple[int, int, int]:
    """Count unique identifier/tag, title/tag term, and body term matches.

    Exact identifiers match title/body identifiers or normalized whole tags. The
    second component matches query terms against title and tags; the third uses
    body, qualifications, and evidence source locators. Evidence excerpts are
    excluded. All-zero scores do not match; disposition is not checked here.
    """
    title_terms, title_ids = tokens(" ".join((revision.title, *revision.tags)))
    body_terms, body_ids = tokens(
        " ".join(
            (
                revision.body,
                *revision.qualifications,
                *(item.source_locator for item in revision.evidence),
            )
        )
    )
    return (
        len(
            set(query.identifiers)
            & (
                set(title_ids)
                | set(body_ids)
                | {normalize(tag) for tag in revision.tags}
            )
        ),
        len(set(query.terms) & set(title_terms)),
        len(set(query.terms) & set(body_terms)),
    )


def selection_order(item: SelectedMemory) -> tuple[int, int, int, str]:
    """Rank scores descending lexicographically, breaking ties by ascending record ID."""
    return (-item.score[0], -item.score[1], -item.score[2], item.record_id)


def select_memory(
    snapshot: MemorySnapshot,
    node_id: str,
    role: Literal["executor", "reviewer"],
    query: MemoryQuery,
    max_records: int = 5,
    max_chars: int = 8000,
    append_after_fragment_count: int = 0,
) -> MemorySelection:
    """Select current accepted matches in rank order without splitting records.

    Pack whole rendered records within both limits, including block framing.
    Skip oversized records and continue trying later matches. Per-record omissions
    retain rank order, with the record limit checked before the character limit;
    any no-context, no-match, or no-fit explanation follows them. Model validation
    errors propagate for invalid limits, query/policy, or frozen selection data.
    The supplied snapshot and query remain unchanged.
    """
    ranked = _rank_matching_revisions(snapshot, query)
    selected, omissions = _pack_ranked_records(ranked, max_records, max_chars)
    omissions.extend(_empty_selection_omissions(query, ranked, selected))
    rendered = render_block(tuple((item.record_id, item.revision) for item in selected))
    return MemorySelection(
        node_id=node_id,
        role=role,
        query=query,
        max_records=max_records,
        max_chars=max_chars,
        append_after_fragment_count=append_after_fragment_count,
        selected=tuple(selected),
        rendered=rendered,
        rendered_chars=len(rendered),
        omissions=tuple(omissions),
    )


def _rank_matching_revisions(
    snapshot: MemorySnapshot, query: MemoryQuery
) -> list[SelectedMemory]:
    """Score only accepted-pointer revisions and sort their nonzero matches."""
    ranked: list[SelectedMemory] = []
    for record in snapshot.records:
        for revision in record.revisions:
            if revision.revision_id != record.accepted_revision:
                continue
            score = score_revision(query, revision)
            if not any(score):
                continue
            ranked.append(
                SelectedMemory(
                    record_id=record.record_id, revision=revision, score=score
                )
            )
    return sorted(ranked, key=selection_order)


def _pack_ranked_records(
    ranked: list[SelectedMemory], max_records: int, max_chars: int
) -> tuple[list[SelectedMemory], list[str]]:
    """Pack complete records and report skipped records in their ranked order."""
    selected: list[SelectedMemory] = []
    omissions: list[str] = []
    for item in ranked:
        if len(selected) == max_records:
            omissions.append(f"{item.record_id}: record limit")
            continue
        candidate = (*selected, item)
        rendered = render_block(
            tuple((entry.record_id, entry.revision) for entry in candidate)
        )
        if len(rendered) > max_chars:
            omissions.append(
                f"{item.record_id}: complete record does not fit character limit"
            )
            continue
        selected.append(item)
    return selected, omissions


def _empty_selection_omissions(
    query: MemoryQuery, ranked: list[SelectedMemory], selected: list[SelectedMemory]
) -> tuple[str, ...]:
    """Explain empty recall with no-context, no-match, then no-fit precedence."""
    if not query.terms:
        return ("Insufficient frozen task context: no eligible query terms.",)
    if not ranked:
        return ("No accepted records match the frozen query.",)
    if not selected:
        return ("No whole matching record fits the recall limits.",)
    return ()

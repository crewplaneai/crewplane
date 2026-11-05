from __future__ import annotations

from typing import Literal

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
    record_id: str
    revision: MemoryRevision
    score: tuple[int, int, int]


class MemorySelection(FrozenModel):
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
    def validate_frozen_selection(self) -> MemorySelection:
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
        return self

    def semantic_payload(self) -> dict[str, object]:
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
    ranked = []
    for record in snapshot.records:
        for revision in record.revisions:
            if revision.revision_id == record.accepted_revision:
                score = score_revision(query, revision)
                if any(score):
                    ranked.append(
                        SelectedMemory(
                            record_id=record.record_id, revision=revision, score=score
                        )
                    )
    selected: list[SelectedMemory] = []
    omissions: list[str] = []
    for item in sorted(ranked, key=selection_order):
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
    if not query.terms:
        omissions.append("Insufficient frozen task context: no eligible query terms.")
    elif not ranked:
        omissions.append("No accepted records match the frozen query.")
    elif not selected:
        omissions.append("No whole matching record fits the recall limits.")
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

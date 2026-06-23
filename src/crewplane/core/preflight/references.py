from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from crewplane.core.workflow.syntax import (
    KEY_VALUE_TEMPLATE_PATTERN,
    NODE_ARTIFACT_REFERENCE_PATTERN,
    TEMPLATE_TOKEN_PATTERN,
)

TemplateReferenceKind = Literal["node", "file", "env", "var", "param", "unknown"]


@dataclass(frozen=True)
class TemplateReference:
    raw_token: str
    start: int
    end: int
    kind: TemplateReferenceKind
    key: str | None = None
    node_id: str | None = None
    artifact_name: str | None = None


def iter_template_references(text: str) -> tuple[TemplateReference, ...]:
    references: list[TemplateReference] = []
    for match in TEMPLATE_TOKEN_PATTERN.finditer(text):
        raw_token = match.group(0)
        node_match = NODE_ARTIFACT_REFERENCE_PATTERN.fullmatch(raw_token)
        if node_match is not None:
            references.append(
                TemplateReference(
                    raw_token=raw_token,
                    start=match.start(),
                    end=match.end(),
                    kind="node",
                    node_id=node_match.group(1),
                    artifact_name=node_match.group(2),
                )
            )
            continue
        key_value_match = KEY_VALUE_TEMPLATE_PATTERN.fullmatch(raw_token)
        if key_value_match is None:
            references.append(
                TemplateReference(
                    raw_token=raw_token,
                    start=match.start(),
                    end=match.end(),
                    kind="unknown",
                )
            )
            continue
        reference_kind = key_value_match.group(1).strip()
        key = key_value_match.group(2).strip()
        if reference_kind in {"file", "env", "var", "param"}:
            references.append(
                TemplateReference(
                    raw_token=raw_token,
                    start=match.start(),
                    end=match.end(),
                    kind=reference_kind,
                    key=key,
                )
            )
            continue
        references.append(
            TemplateReference(
                raw_token=raw_token,
                start=match.start(),
                end=match.end(),
                kind="unknown",
                key=key,
            )
        )
    return tuple(references)

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeIs

from crewplane.core.workflow.syntax import (
    TEMPLATE_TOKEN_PATTERN,
    NodeArtifactToken,
    match_template_token,
)

TemplateReferenceKind = Literal["node", "file", "env", "var", "param", "unknown"]
KeyValueTemplateReferenceKind = Literal["file", "env", "var", "param"]
_KEY_VALUE_REFERENCE_KINDS = frozenset({"file", "env", "var", "param"})


def _is_key_value_reference_kind(
    value: str,
) -> TypeIs[KeyValueTemplateReferenceKind]:
    return value in _KEY_VALUE_REFERENCE_KINDS


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
        token = match_template_token(raw_token)
        if isinstance(token, NodeArtifactToken):
            references.append(
                TemplateReference(
                    raw_token=raw_token,
                    start=match.start(),
                    end=match.end(),
                    kind="node",
                    node_id=token.node_id,
                    artifact_name=token.artifact_name,
                )
            )
            continue
        if token is None:
            references.append(
                TemplateReference(
                    raw_token=raw_token,
                    start=match.start(),
                    end=match.end(),
                    kind="unknown",
                )
            )
            continue
        reference_kind = token.template_type.strip()
        key = token.value.strip()
        if _is_key_value_reference_kind(reference_kind):
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

"""Canonical digest construction for creating and restoring foreach expansions."""

from crewplane.core.expansion_state import ExpansionArtifact

from ..models import CompiledForeach
from ..signatures import signature_for_payload


def expansion_digest(
    template: CompiledForeach,
    workflow_signature: str,
    collection: ExpansionArtifact,
    items: list[ExpansionArtifact],
    caller_artifacts: dict[str, ExpansionArtifact],
) -> str:
    """Hash trusted template identity and artifact evidence without mutation or I/O."""
    return signature_for_payload(
        {
            "workflow_signature": workflow_signature,
            "template_digest": template.template_digest,
            "group_id": template.group_id,
            "collection": collection,
            "items": items,
            "caller_artifacts": caller_artifacts,
            "shared_bindings": template.shared_bindings,
        }
    )

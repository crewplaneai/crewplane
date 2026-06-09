from __future__ import annotations

from pathlib import Path

from ..prompt_segments import PromptSegmentPayload
from ..workflow_models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowProviderPayload,
    workflow_provider_payload_dict,
)
from .models import WorkflowNodeConfig


def read_workflow_text(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def split_frontmatter(markdown_text: str, source: Path) -> tuple[str, str, int]:
    lines = markdown_text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError(
            f"{source} must start with YAML frontmatter delimited by '---' lines."
        )

    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            frontmatter = "".join(lines[1:index])
            body = "".join(lines[index + 1 :])
            return frontmatter, body, index + 1

    raise ValueError(f"{source} is missing the closing frontmatter delimiter '---'.")


def normalize_provider_spec(
    provider: str | ProviderSpec,
    source: Path,
) -> WorkflowProviderPayload:
    if isinstance(provider, str):
        normalized = provider.strip()
        if not normalized:
            raise ValueError(
                f"{source} contains an empty provider name in frontmatter."
            )
        return {"provider": normalized}
    return workflow_provider_payload_dict(provider)


def workflow_node_from_frontmatter(
    node: WorkflowNodeConfig,
    providers: list[WorkflowProviderPayload],
    prompt_segments: list[PromptSegmentPayload],
    source_path: Path,
) -> WorkflowNode:
    if node.mode != "input" and node.source is not None:
        raise ValueError(
            f"{source_path} node '{node.id}' declares source, but source is only "
            "valid for input nodes."
        )
    node_data = node.model_dump(exclude={"providers", "source"}, exclude_unset=True)
    node_data["providers"] = providers
    if node.mode == "input":
        node_data["source"] = node.source or ""
    else:
        node_data["prompt_segments"] = prompt_segments
    return WorkflowNode.model_validate(node_data)

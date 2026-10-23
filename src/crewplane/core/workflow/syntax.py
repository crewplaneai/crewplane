from __future__ import annotations

import re
from dataclasses import dataclass

NODE_ID_PATTERN = re.compile(r"^[a-z0-9._-]+$")
INPUT_SOURCE_PATTERN = re.compile(r"^\{\{\s*file:[^}]+\}\}$")
NODE_ARTIFACT_REFERENCE_PATTERN = re.compile(
    r"\{\{\s*([a-z0-9._-]+)\.([A-Za-z0-9_-]+)\s*\}\}"
)
TEMPLATE_TOKEN_PATTERN = re.compile(r"\{\{[^{}]*\}\}")
KEY_VALUE_TEMPLATE_PATTERN = re.compile(r"\{\{([a-zA-Z_]+):([^}]+)\}\}")
PARAM_TEMPLATE_PATTERN = re.compile(r"\{\{\s*param:([^}]+)\}\}")
SUPPORTED_TEMPLATE_TYPES = frozenset({"env", "file", "var"})
COMPOSITION_ONLY_TEMPLATE_TYPES = frozenset({"param"})


@dataclass(frozen=True)
class NodeArtifactToken:
    node_id: str
    artifact_name: str


@dataclass(frozen=True)
class KeyValueToken:
    template_type: str
    value: str


def match_template_token(token: str) -> NodeArtifactToken | KeyValueToken | None:
    """Match one token without normalizing its spelling or assigning semantics."""
    node_match = NODE_ARTIFACT_REFERENCE_PATTERN.fullmatch(token)
    if node_match is not None:
        return NodeArtifactToken(node_match.group(1), node_match.group(2))
    key_value_match = KEY_VALUE_TEMPLATE_PATTERN.fullmatch(token)
    if key_value_match is not None:
        return KeyValueToken(key_value_match.group(1), key_value_match.group(2))
    return None

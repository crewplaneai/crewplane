from __future__ import annotations

from typing import Literal, get_args

NodeMode = Literal["parallel", "sequential", "input"]
ProviderRole = Literal["executor", "reviewer"]
SequentialConsensusPolicy = Literal["continue", "fatal"]
NodeArtifactName = Literal[
    "output",
    "findings",
    "output_path",
    "findings_path",
    "output_size",
    "findings_size",
    "output_sha256",
    "findings_sha256",
]

ALLOWED_NODE_MODES = get_args(NodeMode)
ALLOWED_PROVIDER_ROLES = get_args(ProviderRole)
ALLOWED_SEQUENTIAL_CONSENSUS_POLICIES = get_args(SequentialConsensusPolicy)
ALLOWED_NODE_ARTIFACT_NAMES = get_args(NodeArtifactName)

ALLOWED_NODE_MODE_SET = frozenset(ALLOWED_NODE_MODES)
ALLOWED_PROVIDER_ROLE_SET = frozenset(ALLOWED_PROVIDER_ROLES)
ALLOWED_SEQUENTIAL_CONSENSUS_POLICY_SET = frozenset(
    ALLOWED_SEQUENTIAL_CONSENSUS_POLICIES
)
ALLOWED_NODE_ARTIFACT_NAME_SET = frozenset(ALLOWED_NODE_ARTIFACT_NAMES)
RESERVED_RUN_ROOT_NAMES = frozenset({"logs", "manifests", "workspace-exports"})


def validate_exact_keyword(
    value: object,
    field_name: str,
    allowed_values: tuple[str, ...],
    allowed_value_set: frozenset[str],
) -> object:
    if not isinstance(value, str):
        return value

    if value in allowed_value_set:
        return value

    lowered = value.lower()
    allowed = ", ".join(allowed_values)
    if lowered in allowed_value_set:
        raise ValueError(f"{field_name} must be lower-case and one of: {allowed}")
    raise ValueError(f"{field_name} must be one of: {allowed}")

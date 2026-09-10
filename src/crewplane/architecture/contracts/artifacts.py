from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

MAX_ARTIFACT_PATH_COMPONENT_CHARS = 180
REVIEW_AUDIT_DIRECTORY_PREFIX = "review-audit-round-"
_STAGE_PATTERN = re.compile(r"[^a-z0-9._-]+")
_ARTIFACT_PATTERN = re.compile(r"[^a-z0-9]+")


class ArtifactContract(BaseModel):
    """Compiled, adapter-neutral locations for one node's artifacts."""

    model_config = ConfigDict(extra="forbid")

    stage_path: str | None = None
    output_path: str
    findings_path: str | None = None
    log_path: str | None = None
    manifest_path: str | None = None
    result_path: str | None = None

    @field_validator(
        "stage_path",
        "output_path",
        "findings_path",
        "log_path",
        "manifest_path",
        "result_path",
    )
    @classmethod
    def _validate_relative_locator(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if (
            not value.strip()
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("Artifact locators must be nonblank safe relative paths.")
        return path.as_posix()


@dataclass(frozen=True)
class NodeArtifactRequest:
    """Node identity paired with its compiled artifact locations."""

    node_id: str
    contract: ArtifactContract

    def __post_init__(self) -> None:
        if not self.node_id.strip():
            raise ValueError("Artifact requests require a nonblank node_id.")
        if self.contract.stage_path is None:
            raise ValueError("Artifact requests require a stage_path locator.")


@dataclass(frozen=True)
class VerifiedNodeArtifact:
    """Artifact bytes verified against the persisted node descriptor."""

    path: Path
    payload: bytes
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if self.size_bytes != len(self.payload):
            raise ValueError("Verified artifact size does not match its payload.")
        if hashlib.sha256(self.payload).hexdigest() != self.sha256:
            raise ValueError("Verified artifact hash does not match its payload.")


def artifact_contract_for_node(
    node_id: str,
    findings_enabled: bool,
) -> ArtifactContract:
    stage_path = build_stage_directory_name(node_id)
    output_path = build_result_filename(node_id)
    return ArtifactContract(
        stage_path=stage_path,
        output_path=output_path,
        findings_path=(build_findings_filename(node_id) if findings_enabled else None),
        log_path=f"{stage_path}/logs",
        result_path=output_path,
    )


def build_stage_directory_name(node_id: str) -> str:
    safe_name = safe_stage_name(node_id)
    if len(safe_name) <= MAX_ARTIFACT_PATH_COMPONENT_CHARS:
        return safe_name
    return bounded_artifact_name(safe_name, f"--{artifact_name_hash(node_id)}")


def build_result_filename(node_id: str) -> str:
    return _bounded_artifact_filename(node_id, "-result.md")


def build_findings_filename(node_id: str) -> str:
    return _bounded_artifact_filename(node_id, "-findings.md")


def build_review_audit_directory_name(audit_round_num: int) -> str:
    return f"{REVIEW_AUDIT_DIRECTORY_PREFIX}{audit_round_num}"


def build_task_round_filename(task_id: str, round_num: int) -> str:
    return f"{task_id}_round{round_num}.md"


def safe_artifact_name(name: str) -> str:
    stripped = name.strip().lower()
    if not stripped or stripped in {".", ".."}:
        return "task"
    slug = _ARTIFACT_PATTERN.sub("-", stripped).strip("-")
    return slug or "task"


def _bounded_artifact_filename(node_id: str, suffix: str) -> str:
    safe_name = safe_stage_name(node_id)
    if len(f"{safe_name}{suffix}") <= MAX_ARTIFACT_PATH_COMPONENT_CHARS:
        return f"{safe_name}{suffix}"
    return bounded_artifact_name(safe_name, f"--{artifact_name_hash(node_id)}{suffix}")


def bounded_artifact_name(safe_prefix: str, suffix: str) -> str:
    if len(suffix) >= MAX_ARTIFACT_PATH_COMPONENT_CHARS:
        raise ValueError("Generated suffix exceeds path component budget.")
    available = MAX_ARTIFACT_PATH_COMPONENT_CHARS - len(suffix)
    prefix = safe_prefix[:available].rstrip("-._")
    if not prefix:
        prefix = "artifact"[:available]
    return f"{prefix}{suffix}"


def safe_stage_name(name: str) -> str:
    stripped = name.strip().lower()
    if not stripped or stripped in {".", ".."}:
        return "task"
    slug = _STAGE_PATTERN.sub("-", stripped)
    if slug in {".", ".."}:
        return "task"
    if not slug.strip("-._"):
        return slug
    return slug or "task"


def artifact_name_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]

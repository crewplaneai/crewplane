from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    field_validator,
    model_validator,
)

from crewplane.core.workflow.keywords import validate_exact_keyword
from crewplane.core.workflow.syntax import NODE_ID_PATTERN
from crewplane.version import SCHEMA_VERSION

WorktreeKind = Literal["worktree", "snapshot"]
WorktreeContractMode = Literal["blob_exact"]
WorkspaceCleanStart = Literal["strict", "tracked_only"]
WorkspaceMaterialization = Literal[
    "project_root",
    "snapshot_checkout",
    "worktree_checkout",
]
WorkspaceSourceKind = Literal["project", "node"]

WORKTREE_CONTRACT_MODES: tuple[WorktreeContractMode, ...] = ("blob_exact",)
WORKTREE_CONTRACT_MODE_SET = frozenset(WORKTREE_CONTRACT_MODES)
WORKTREE_KINDS: tuple[WorktreeKind, ...] = ("worktree", "snapshot")
WORKTREE_KIND_SET = frozenset(WORKTREE_KINDS)
WORKSPACE_CLEAN_START_VALUES: tuple[WorkspaceCleanStart, ...] = (
    "strict",
    "tracked_only",
)
WORKSPACE_CLEAN_START_SET = frozenset(WORKSPACE_CLEAN_START_VALUES)
PROJECT_ROOT_WORKTREE_SELECTOR = "none"
INVALID_BRANCH_REF_CHARS = frozenset(" ~^:?*[\\")
MAX_REF_COMPONENT_CHARS = 96
SAFE_COMPONENT_HASH_CHARS = 12
FALLBACK_HASH_CHARS = 16


class WorktreeContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: WorktreeContractMode = "blob_exact"
    schema_version: str = SCHEMA_VERSION


class WorktreeDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: WorktreeKind
    setup_profile: str | None = None
    create_branch: StrictBool = False
    branch_name: str | None = None

    @field_validator("kind", mode="before")
    @classmethod
    def _validate_kind(cls, value: object) -> object:
        return validate_exact_keyword(
            value,
            field_name="worktree kind",
            allowed_values=WORKTREE_KINDS,
            allowed_value_set=WORKTREE_KIND_SET,
        )

    @field_validator("setup_profile", mode="before")
    @classmethod
    def _validate_setup_profile_name(cls, value: object) -> object:
        if value is None:
            return value
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("setup_profile cannot be blank")
        return normalized

    @field_validator("branch_name", mode="before")
    @classmethod
    def _validate_branch_name(cls, value: object) -> object:
        if value is None:
            return value
        if not isinstance(value, str):
            return value
        return validate_branch_name(value)

    @model_validator(mode="after")
    def _validate_kind_options(self) -> WorktreeDeclaration:
        if self.kind == "snapshot":
            if self.setup_profile is not None:
                raise ValueError("snapshot worktrees cannot declare setup_profile")
            if self.create_branch:
                raise ValueError("snapshot worktrees cannot declare create_branch")
            if self.branch_name is not None:
                raise ValueError("snapshot worktrees cannot declare branch_name")
        if self.branch_name is not None and not self.create_branch:
            raise ValueError("branch_name requires create_branch: true")
        if self.branch_name is not None:
            validate_branch_name(self.branch_name)
        return self


def validate_worktree_name(name: str) -> str:
    normalized = name.strip()
    if normalized == PROJECT_ROOT_WORKTREE_SELECTOR:
        raise ValueError("'none' is reserved and cannot be a worktree name")
    if not NODE_ID_PATTERN.fullmatch(normalized):
        raise ValueError("worktree names must match '[a-z0-9._-]+'")
    return normalized


def validate_branch_name(name: str) -> str:
    normalized = name.strip()
    if normalized != name or not normalized:
        raise ValueError("branch_name cannot be blank or padded with whitespace")
    if normalized in {"@", "HEAD"} or normalized.startswith(("refs/", "-")):
        raise ValueError("branch_name must be a branch name, not a ref path")
    ref = f"refs/heads/{normalized}"
    if (
        ref.endswith("/")
        or ref.endswith(".")
        or ".." in ref
        or "//" in ref
        or "@{" in ref
    ):
        raise ValueError("branch_name is not a valid Git branch name")
    if any(
        ord(char) < 32 or ord(char) == 127 or char in INVALID_BRANCH_REF_CHARS
        for char in ref
    ):
        raise ValueError("branch_name contains characters Git refs do not allow")
    parts = ref.split("/")
    if not all(
        part
        and part not in {".", ".."}
        and not part.startswith(".")
        and not part.endswith(".")
        and not part.endswith(".lock")
        for part in parts
    ):
        raise ValueError("branch_name is not a valid Git branch name")
    return normalized


def generated_branch_name(
    workflow_name: str,
    logical_worktree_name: str,
    run_key_name: str,
) -> str:
    return (
        "crewplane/"
        f"{safe_ref_component(workflow_name)}/"
        f"{safe_ref_component(logical_worktree_name)}/"
        f"{safe_ref_component(run_key_name)}"
    )


def safe_ref_component(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-/")
    while ".." in slug:
        slug = slug.replace("..", ".")
    if not slug or slug.endswith(".") or slug.endswith(".lock"):
        slug = _fallback_hash(value)
    return _bounded_component(slug, value, MAX_REF_COMPONENT_CHARS, "-")


def _bounded_component(
    slug: str,
    original: str,
    max_chars: int,
    separator: str,
) -> str:
    if len(slug) <= max_chars:
        return slug
    suffix = f"{separator}{_short_hash(original)}"
    available = max_chars - len(suffix)
    prefix = slug[:available].rstrip(".-")
    if not prefix:
        prefix = _fallback_hash(original)[:available]
    return f"{prefix}{suffix}"


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:SAFE_COMPONENT_HASH_CHARS]


def _fallback_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:FALLBACK_HASH_CHARS]


def worktree_declarations_payload(
    declarations: dict[str, WorktreeDeclaration],
) -> dict[str, dict[str, str | bool]]:
    payload: dict[str, dict[str, str | bool]] = {}
    for name, declaration in declarations.items():
        item: dict[str, str | bool] = {"kind": declaration.kind}
        if declaration.setup_profile is not None:
            item["setup_profile"] = declaration.setup_profile
        if declaration.create_branch:
            item["create_branch"] = True
        if declaration.branch_name is not None:
            item["branch_name"] = declaration.branch_name
        payload[name] = item
    return payload


def default_worktree_contract() -> WorktreeContract:
    return WorktreeContract(mode="blob_exact", schema_version=SCHEMA_VERSION)

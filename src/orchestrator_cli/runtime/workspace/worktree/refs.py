from __future__ import annotations

import hashlib
import re
from pathlib import Path

from orchestrator_cli.core.workspace_policy import (
    safe_ref_component as core_safe_ref_component,
)

from ..git import git

MAX_FILE_COMPONENT_CHARS = 120
SAFE_COMPONENT_HASH_CHARS = 12
FALLBACK_HASH_CHARS = 16


def checked_ref(checkout_root: Path, ref: str) -> str:
    normalized = git(checkout_root).text("check-ref-format", "--normalize", ref)
    if normalized != ref:
        raise RuntimeError(f"Unsafe workspace ref name: {ref}")
    return normalized


def safe_ref_component(value: str) -> str:
    return core_safe_ref_component(value)


def safe_file_component(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if not slug:
        slug = _fallback_hash(value)
    return _bounded_component(slug, value, MAX_FILE_COMPONENT_CHARS, "--")


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

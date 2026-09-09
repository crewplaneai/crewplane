from __future__ import annotations

import hashlib
import re

MAX_REF_COMPONENT_CHARS = 96
MAX_FILE_COMPONENT_CHARS = 120
SAFE_COMPONENT_HASH_CHARS = 12
FALLBACK_HASH_CHARS = 16


def safe_ref_component(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-/")
    while ".." in slug:
        slug = slug.replace("..", ".")
    if not slug or slug.endswith(".") or slug.endswith(".lock"):
        slug = _fallback_hash(value)
    return _bounded_component(slug, value, MAX_REF_COMPONENT_CHARS, "-")


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


def result_ref_names(run_key_name: str, node_id: str, slug: str) -> tuple[str, str]:
    base = (
        "refs/crewplane/runs/"
        f"{safe_ref_component(run_key_name)}/"
        f"{safe_ref_component(node_id)}/"
        f"{safe_ref_component(slug)}"
    )
    return f"{base}/candidate", f"{base}/result"


def temporary_import_ref_prefix(run_key_name: str, node_id: str, slug: str) -> str:
    return (
        "refs/crewplane/runs/"
        f"{safe_ref_component(run_key_name)}/imports/"
        f"{safe_ref_component(node_id)}/"
        f"{safe_ref_component(slug)}/"
    )

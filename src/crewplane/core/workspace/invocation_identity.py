from __future__ import annotations

import hashlib
import re

MAX_INVOCATION_SLUG_CHARS = 160
INVOCATION_SLUG_HASH_CHARS = 12


def invocation_slug(
    node_id: str,
    task_id: str,
    audit_round_num: int | None,
    round_num: int,
) -> str:
    audit = f"audit{audit_round_num}-" if audit_round_num is not None else ""
    return bounded_invocation_slug(f"{node_id}-{task_id}-{audit}round{round_num}")


def bounded_invocation_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if not slug:
        return _short_slug_hash(value)
    if len(slug) <= MAX_INVOCATION_SLUG_CHARS:
        return slug
    suffix = f"--{_short_slug_hash(value)}"
    prefix = slug[: MAX_INVOCATION_SLUG_CHARS - len(suffix)].rstrip(".-")
    return f"{prefix or 'workspace'}{suffix}"


def _short_slug_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[
        :INVOCATION_SLUG_HASH_CHARS
    ]

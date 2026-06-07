from __future__ import annotations

import hashlib
from typing import Any

from .serialization import canonical_json_bytes


def signature_for_payload(payload: Any) -> str:
    """Hash canonical JSON payloads with SHA-256."""

    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

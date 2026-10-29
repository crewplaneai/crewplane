import hashlib
import re
from pathlib import Path

from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.architecture.safe_files import contained_regular_file
from crewplane.core.preflight.models import PreflightExecutionPlan


def read_verified_static_text(plan: PreflightExecutionPlan, content_ref: str) -> str:
    """Read the exact UTF-8 bytes described by a compiled static resource."""
    resources = [
        resource
        for resource in plan.static_resources
        if resource.content_ref == content_ref
    ]
    context = f"Static resource '{content_ref}'"
    if len(resources) != 1:
        raise ValueError(f"{context} requires exactly one descriptor.")
    resource = resources[0]
    if (
        resource.encoding != "utf-8"
        or type(resource.size_bytes) is not int
        or resource.size_bytes < 0
        or not isinstance(resource.sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", resource.sha256) is None
    ):
        raise ValueError(f"{context} has invalid encoding, size, or SHA-256 metadata.")
    try:
        payload = read_contained_bytes(
            Path(plan.context_root) / "preflight", content_ref, resource.size_bytes
        )
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"{context} is missing or unsafe, or exceeds its size."
        ) from exc
    if (
        len(payload) != resource.size_bytes
        or hashlib.sha256(payload).hexdigest() != resource.sha256
    ):
        raise ValueError(f"{context} bytes do not match its descriptor.")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{context} is not valid UTF-8.") from None
    if "\x00" in text:
        raise ValueError(f"{context} contains NUL content.")
    return text


def preflight_content_path(
    context_root: str, content_ref: str, content_kind: str
) -> Path | None:
    """Look up a compiled bundle file without choosing a read or missing-file policy."""
    normalized_ref = Path(content_ref)
    if normalized_ref.is_absolute() or ".." in normalized_ref.parts:
        raise ValueError(f"Invalid {content_kind} content reference '{content_ref}'.")
    return contained_regular_file(
        Path(context_root) / "preflight", normalized_ref.as_posix()
    )

from __future__ import annotations

from crewplane.core.workspace.git_policy import GIT_OBJECT_FORMAT_DIGEST_SIZES

from .git_source import GitSourceContext
from .source_types import WorkspacePolicyBuilder

ALLOWED_INDEX_EXTENSIONS = {"TREE", "REUC", "EOIE", "IEOT"}
UNSUPPORTED_INDEX_EXTENSIONS = {
    "link": "split-index",
    "UNTR": "untracked-cache",
    "FSMN": "fsmonitor",
    "sdir": "sparse-index",
}
SUPPORTED_GIT_INDEX_FORMAT_VERSIONS = {2, 3, 4}
INDEX_HEADER_SIZE = 12
INDEX_SIGNATURE_SIZE = 4
INDEX_VERSION_END = 8
ENTRY_STAT_SIZE = 40
ENTRY_FLAGS_SIZE = 2
ENTRY_EXTENDED_FLAGS_SIZE = 2
ENTRY_ALIGNMENT = 8
ENTRY_EXTENDED_FLAGS_MASK = 0x4000
V4_PREFIX_CONTINUATION_MASK = 0x80
EXTENSION_SIGNATURE_SIZE = 4
EXTENSION_HEADER_SIZE = 8


def validate_index_extensions(
    git_context: GitSourceContext,
    builder: WorkspacePolicyBuilder,
) -> None:
    index_path = git_context.active_git_dir / "index"
    try:
        extensions = git_index_extensions(
            index_path.read_bytes(),
            git_context.object_format,
        )
    except FileNotFoundError:
        builder.errors.append("Workspace source policy failed: Git index is missing.")
        return
    except (OSError, ValueError) as exc:
        builder.errors.append(
            "Workspace source policy failed: Git index is corrupt or unsupported "
            f"({exc})."
        )
        return
    unsupported = sorted(
        UNSUPPORTED_INDEX_EXTENSIONS[extension]
        for extension in extensions
        if extension in UNSUPPORTED_INDEX_EXTENSIONS
    )
    unknown = sorted(
        extension
        for extension in extensions
        if extension not in ALLOWED_INDEX_EXTENSIONS
        and extension not in UNSUPPORTED_INDEX_EXTENSIONS
    )
    if unsupported:
        builder.errors.append(
            "Workspace source policy failed: Git index contains unsupported "
            f"state: {', '.join(unsupported)}. Disable that index feature before "
            "enabling workspace isolation, or set settings.workspace.enabled: "
            "false for this run."
        )
    if unknown:
        builder.errors.append(
            "Workspace source policy failed: Git index contains unsupported "
            f"extensions: {', '.join(unknown)}. Use a standard Git index before "
            "enabling workspace isolation, or set settings.workspace.enabled: "
            "false for this run."
        )


def git_index_extensions(payload: bytes, object_format: str) -> tuple[str, ...]:
    checksum_length = index_checksum_length(object_format)
    if len(payload) < INDEX_HEADER_SIZE + checksum_length:
        raise ValueError("index file is too short")
    if payload[:INDEX_SIGNATURE_SIZE] != b"DIRC":
        raise ValueError("index header is invalid")
    index_format_version = int.from_bytes(
        payload[INDEX_SIGNATURE_SIZE:INDEX_VERSION_END], "big"
    )
    if index_format_version not in SUPPORTED_GIT_INDEX_FORMAT_VERSIONS:
        raise ValueError(
            f"Git index format version {index_format_version} is unsupported"
        )
    entry_count = int.from_bytes(payload[INDEX_VERSION_END:INDEX_HEADER_SIZE], "big")
    offset = INDEX_HEADER_SIZE
    extension_end = len(payload) - checksum_length
    entries_remaining = entry_count
    while entries_remaining:
        offset = next_index_entry_offset(
            payload,
            offset,
            index_format_version,
            checksum_length,
            extension_end,
        )
        entries_remaining -= 1
    return read_index_extensions(payload, offset, extension_end)


def index_checksum_length(object_format: str) -> int:
    try:
        return GIT_OBJECT_FORMAT_DIGEST_SIZES[object_format]
    except KeyError as exc:
        raise ValueError(f"object format {object_format!r} is unsupported") from exc


def next_index_entry_offset(
    payload: bytes,
    offset: int,
    index_format_version: int,
    checksum_length: int,
    extension_end: int,
) -> int:
    entry_start = offset
    fixed_length = ENTRY_STAT_SIZE + checksum_length + ENTRY_FLAGS_SIZE
    if offset + fixed_length > extension_end:
        raise ValueError("index entry is truncated")
    flags = int.from_bytes(
        payload[offset + fixed_length - ENTRY_FLAGS_SIZE : offset + fixed_length],
        "big",
    )
    offset += fixed_length
    if flags & ENTRY_EXTENDED_FLAGS_MASK:
        if offset + ENTRY_EXTENDED_FLAGS_SIZE > extension_end:
            raise ValueError("extended index entry flags are truncated")
        offset += ENTRY_EXTENDED_FLAGS_SIZE
    if index_format_version == 4:
        offset = skip_v4_path_prefix_length(payload, offset, extension_end)
    path_end = payload.find(b"\0", offset, extension_end)
    if path_end < 0:
        raise ValueError("index entry path is unterminated")
    entry_end = path_end + 1
    if index_format_version == 4:
        return entry_end
    padding = (
        ENTRY_ALIGNMENT - ((entry_end - entry_start) % ENTRY_ALIGNMENT)
    ) % ENTRY_ALIGNMENT
    if entry_end + padding > extension_end:
        raise ValueError("index entry padding is truncated")
    return entry_end + padding


def skip_v4_path_prefix_length(
    payload: bytes,
    offset: int,
    extension_end: int,
) -> int:
    while offset < extension_end:
        value = payload[offset]
        offset += 1
        if value & V4_PREFIX_CONTINUATION_MASK == 0:
            return offset
    raise ValueError("index v4 path prefix length is truncated")


def read_index_extensions(
    payload: bytes,
    offset: int,
    extension_end: int,
) -> tuple[str, ...]:
    extensions: list[str] = []
    while offset < extension_end:
        if offset + EXTENSION_HEADER_SIZE > extension_end:
            raise ValueError("index extension header is truncated")
        signature = payload[offset : offset + EXTENSION_SIGNATURE_SIZE].decode(
            "ascii", errors="replace"
        )
        size = int.from_bytes(
            payload[offset + EXTENSION_SIGNATURE_SIZE : offset + EXTENSION_HEADER_SIZE],
            "big",
        )
        offset += EXTENSION_HEADER_SIZE
        if offset + size > extension_end:
            raise ValueError(f"index extension {signature!r} is truncated")
        extensions.append(signature)
        offset += size
    return tuple(extensions)

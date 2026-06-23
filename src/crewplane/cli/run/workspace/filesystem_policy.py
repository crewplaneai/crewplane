from __future__ import annotations

import os
import stat
import tempfile
import unicodedata
from pathlib import Path

from crewplane.core.config import Settings
from crewplane.core.workspace.cache import workspace_cache_root

from .source_types import WorkspacePolicyBuilder


def probe_filesystem_capabilities(
    settings: Settings,
    builder: WorkspacePolicyBuilder,
) -> dict[str, bool]:
    try:
        capabilities = _probe_capabilities(
            workspace_cache_root(settings.workspace.cache_root)
        )
    except OSError as exc:
        builder.errors.append(
            "Workspace source policy failed: cache filesystem capability probe "
            f"could not run ({exc})."
        )
        return {}
    if not capabilities.get("executable_bit", False):
        builder.errors.append(
            "Workspace source policy failed: cache filesystem does not preserve "
            "POSIX executable-bit semantics required by blob_exact."
        )
    if not capabilities.get("symlink", False):
        builder.errors.append(
            "Workspace source policy failed: cache filesystem does not support "
            "symlinks required by blob_exact."
        )
    return capabilities


def _probe_capabilities(cache_root: Path) -> dict[str, bool]:
    probe_parent = existing_probe_parent(cache_root)
    with tempfile.TemporaryDirectory(
        prefix="crewplane-fs-probe-",
        dir=probe_parent,
    ) as temp_dir:
        root = Path(temp_dir)
        executable_bit = executable_bit_supported(root)
        symlink = symlink_supported(root)
        case_sensitive = case_sensitive_paths(root)
        unicode_normalization_sensitive = unicode_normalization_sensitive_paths(root)
    return {
        "executable_bit": executable_bit,
        "symlink": symlink,
        "case_sensitive": case_sensitive,
        "unicode_normalization_sensitive": unicode_normalization_sensitive,
    }


def existing_probe_parent(cache_root: Path) -> Path:
    current = cache_root if cache_root.exists() else cache_root.parent
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def executable_bit_supported(root: Path) -> bool:
    path = root / "exec-probe"
    path.touch()
    path.chmod(0o700)
    return bool(stat.S_IMODE(path.stat().st_mode) & stat.S_IXUSR)


def symlink_supported(root: Path) -> bool:
    target = root / "symlink-target"
    link = root / "symlink-link"
    target.write_text("target\n", encoding="utf-8")
    try:
        os.symlink(target.name, link)
    except OSError:
        return False
    return link.is_symlink() and os.readlink(link) == target.name


def case_sensitive_paths(root: Path) -> bool:
    path = root / "case-probe"
    path.write_text("lower\n", encoding="utf-8")
    return not (root / "CASE-PROBE").exists()


def unicode_normalization_sensitive_paths(root: Path) -> bool:
    decomposed = "Cafe\u0301-probe"
    composed = unicodedata.normalize("NFC", decomposed)
    path = root / decomposed
    path.write_text("unicode\n", encoding="utf-8")
    return not (root / composed).exists() or decomposed == composed

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from json import JSONDecodeError
from pathlib import Path


def artifact_path_label(stages_dir: Path, value: str | None) -> str | None:
    if value is None:
        return None
    return artifact_relative_path(stages_dir, Path(value))


def artifact_relative_path(root: Path, path: Path) -> str:
    try:
        return (
            path.resolve(strict=False)
            .relative_to(root.resolve(strict=False))
            .as_posix()
        )
    except ValueError:
        return path.as_posix() if not path.is_absolute() else path.name


def state_relative_artifact_path(
    stages_dir: Path,
    state_path: Path,
    value: object,
) -> str | None:
    path_text = string_or_none(value)
    if path_text is None:
        return None
    path = Path(path_text)
    if path.is_absolute():
        return artifact_relative_path(stages_dir, path)
    return artifact_relative_path(stages_dir, state_path.parent / path)


def bundle_path_label(value: object) -> str | None:
    path_text = string_or_none(value)
    if path_text is None:
        return None
    path = Path(path_text)
    if path.is_absolute():
        return path.name
    if any(part in {"", ".", ".."} for part in path.parts):
        return path.name
    return path.as_posix()


def read_json_mapping(path: Path) -> Mapping[str, object] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, JSONDecodeError):
        return None
    return mapping_or_none(payload)


def mapping_or_none(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def bool_or_none(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def list_count_or_none(value: object) -> int | None:
    return len(value) if isinstance(value, list) else None


def section_value[T](
    section: Mapping[str, object] | None,
    field_name: str,
    coerce: Callable[[object], T | None],
) -> T | None:
    if section is None:
        return None
    return coerce(section.get(field_name))


def first_available_section_value[T](
    section: Mapping[str, object] | None,
    field_names: tuple[str, ...],
    coerce: Callable[[object], T | None],
) -> T | None:
    if section is None:
        return None
    for field_name in field_names:
        value = coerce(section.get(field_name))
        if value is not None:
            return value
    return None


def reset_verification_status(reuse: Mapping[str, object] | None) -> str | None:
    if reuse is None:
        return None
    strategy = string_or_none(reuse.get("strategy"))
    fallback = bool_or_none(reuse.get("fallback"))
    reused = bool_or_none(reuse.get("reused"))
    if strategy == "incremental_reset" and reused is True and fallback is False:
        return "verified"
    if fallback is True:
        return "failed_fallback"
    if strategy == "fresh_checkout":
        return "not_applicable"
    return None

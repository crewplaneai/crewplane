from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from orchestrator_cli.core.config import DEFAULT_MOCK_INVOKER_OBSERVATION_DELAY_SECONDS

from .selectors import FailSelector, validate_fail_selectors

type OutputMode = Literal["lorem", "echo", "file"]


@dataclass(frozen=True)
class MockOptions:
    delay_seconds: float
    observation_delay_seconds: float
    output_mode: OutputMode
    output_dir: Path | None
    strict_file_mode: bool
    seed: int | None
    fail_when: tuple[FailSelector, ...]


def _validate_non_negative_number(value: object, option_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"mock invoker option '{option_name}' must be a number >= 0")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"mock invoker option '{option_name}' must be finite")
    if number < 0:
        raise ValueError(f"mock invoker option '{option_name}' must be >= 0")
    return number


def _validate_bool(value: object, option_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"mock invoker option '{option_name}' must be a boolean")
    return value


def _validate_optional_int(value: object, option_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"mock invoker option '{option_name}' must be an integer or null"
        )
    return value


def _validate_output_mode(value: object) -> OutputMode:
    if not isinstance(value, str):
        raise ValueError("mock invoker option 'output_mode' must be a string")
    normalized = value.strip().lower()
    if normalized not in {"lorem", "echo", "file"}:
        raise ValueError(
            "mock invoker option 'output_mode' must be one of: lorem, echo, file"
        )
    return cast(OutputMode, normalized)


def _validate_output_dir(value: object) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "mock invoker option 'output_dir' must be a non-empty string or null"
        )
    return Path(value).resolve()


def parse_options(options: Mapping[str, Any] | None) -> MockOptions:
    resolved = dict(options or {})
    for raw_key in resolved:
        if not isinstance(raw_key, str):
            raise ValueError(
                "mock invoker option keys must be strings; "
                f"got key {raw_key!r} ({type(raw_key).__name__})"
            )
    unknown = sorted(
        set(resolved)
        - {
            "delay_seconds",
            "observation_delay_seconds",
            "output_mode",
            "output_dir",
            "strict_file_mode",
            "seed",
            "fail_when",
        }
    )
    if unknown:
        raise ValueError(
            f"Unsupported mock invoker options: {', '.join(sorted(unknown))}"
        )

    delay_seconds = _validate_non_negative_number(
        resolved.pop("delay_seconds", 0),
        "delay_seconds",
    )
    observation_delay_seconds = _validate_non_negative_number(
        resolved.pop(
            "observation_delay_seconds",
            DEFAULT_MOCK_INVOKER_OBSERVATION_DELAY_SECONDS,
        ),
        "observation_delay_seconds",
    )
    output_mode = _validate_output_mode(resolved.pop("output_mode", "lorem"))
    output_dir = _validate_output_dir(resolved.pop("output_dir", None))
    strict_file_mode = _validate_bool(
        resolved.pop("strict_file_mode", False),
        "strict_file_mode",
    )
    seed = _validate_optional_int(resolved.pop("seed", None), "seed")
    fail_when = validate_fail_selectors(resolved.pop("fail_when", []))

    if output_mode == "file" and output_dir is None:
        raise ValueError(
            "mock invoker option 'output_dir' is required when output_mode='file'"
        )

    return MockOptions(
        delay_seconds=delay_seconds,
        observation_delay_seconds=observation_delay_seconds,
        output_mode=output_mode,
        output_dir=output_dir,
        strict_file_mode=strict_file_mode,
        seed=seed,
        fail_when=fail_when,
    )

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .outputs import OutputResolution


@dataclass(frozen=True)
class FixtureMutation:
    target: Path
    content: str


def build_fixture_mutation_plan(
    resolution: OutputResolution,
    output_file: Path,
) -> tuple[FixtureMutation, ...]:
    if resolution.fixture_path is None:
        return ()
    mutations_path = resolution.fixture_path.with_suffix(".mutations.json")
    if not mutations_path.is_file():
        return ()
    try:
        raw_mutations = json.loads(mutations_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"mock invoker could not parse mutation fixture '{mutations_path}'"
        ) from exc
    if not isinstance(raw_mutations, list):
        raise RuntimeError(
            "mock invoker mutation fixture must be a list of path/content writes; "
            f"got {type(raw_mutations).__name__}"
        )

    mutation_root = output_file.parent.resolve()
    mutation_plan: list[FixtureMutation] = []
    for index, raw_mutation in enumerate(raw_mutations):
        if not isinstance(raw_mutation, dict):
            raise RuntimeError(
                "mock invoker mutation entries must be objects; "
                f"entry[{index}] is {type(raw_mutation).__name__}"
            )
        raw_path = raw_mutation.get("path")
        content = raw_mutation.get("content")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise RuntimeError(
                "mock invoker mutation entry path must be a non-empty string"
            )
        if not isinstance(content, str):
            raise RuntimeError("mock invoker mutation entry content must be a string")
        target = (mutation_root / raw_path).resolve()
        if not target.is_relative_to(mutation_root):
            raise RuntimeError(
                "mock invoker mutation path must stay within the invocation "
                f"output directory: {raw_path}"
            )
        mutation_plan.append(FixtureMutation(target=target, content=content))
    return tuple(mutation_plan)


def apply_fixture_mutations(mutation_plan: tuple[FixtureMutation, ...]) -> None:
    for mutation in mutation_plan:
        target = mutation.target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(mutation.content, encoding="utf-8")

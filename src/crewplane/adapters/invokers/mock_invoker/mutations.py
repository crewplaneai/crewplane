from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from crewplane.architecture.contracts import InvocationContext, JsonObject

from .outputs import OutputResolution


@dataclass(frozen=True)
class FixtureMutation:
    target: Path
    content: str


@dataclass(frozen=True)
class FixtureMutationPlan:
    mutations: tuple[FixtureMutation, ...]


def build_fixture_mutation_plan(
    resolution: OutputResolution,
    output_file: Path,
    cwd: Path,
    context: InvocationContext | None,
    prompt: str,
) -> FixtureMutationPlan:
    """Build output and workspace mutations declared beside a mock fixture.

    Fixture-backed mock outputs may include a sibling `.mutations.json` sidecar.
    The sidecar can declare files to write under the invocation output
    directory, files to write under the invocation working directory after
    workspace validation, and prompt substring requirements that must hold
    before any mutations are applied. Missing sidecars are treated as an empty
    mutation plan.
    """
    if resolution.fixture_path is None:
        return FixtureMutationPlan(mutations=())
    mutations_path = resolution.fixture_path.with_suffix(".mutations.json")
    if not mutations_path.is_file():
        return FixtureMutationPlan(mutations=())
    try:
        raw_sidecar = json.loads(mutations_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"mock invoker could not parse mutation fixture '{mutations_path}'"
        ) from exc
    sidecar = _normalize_sidecar(raw_sidecar)
    _validate_prompt_requirements(sidecar, prompt)
    return FixtureMutationPlan(
        mutations=_fixture_mutations(sidecar, output_file, cwd, context),
    )


def apply_fixture_mutations(plan: FixtureMutationPlan) -> None:
    for mutation in plan.mutations:
        target = mutation.target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(mutation.content, encoding="utf-8")


def _normalize_sidecar(raw_sidecar: object) -> JsonObject:
    if isinstance(raw_sidecar, list):
        return cast(JsonObject, {"mutations": raw_sidecar})
    if not isinstance(raw_sidecar, dict):
        raise RuntimeError(
            "mock invoker mutation fixture must be a list or object; "
            f"got {type(raw_sidecar).__name__}"
        )
    unknown = sorted(
        set(raw_sidecar)
        - {
            "mutations",
            "workspace_mutations",
            "required_prompt_contains",
            "forbidden_prompt_contains",
        }
    )
    if unknown:
        raise RuntimeError(
            f"mock invoker mutation fixture has unsupported keys: {', '.join(unknown)}"
        )
    return cast(JsonObject, dict(raw_sidecar))


def _fixture_mutations(
    sidecar: JsonObject,
    output_file: Path,
    cwd: Path,
    context: InvocationContext | None,
) -> tuple[FixtureMutation, ...]:
    mutation_root = output_file.parent.resolve()
    workspace_mutations = _entry_list(
        sidecar.get("workspace_mutations"),
        "workspace_mutations",
    )
    mutation_plan: list[FixtureMutation] = []
    mutation_plan.extend(
        _mutation_entries(
            raw_mutations=_entry_list(sidecar.get("mutations"), "mutations"),
            mutation_root=mutation_root,
            root_label="output",
            path_key="path",
        )
    )
    mutation_plan.extend(
        _mutation_entries(
            raw_mutations=workspace_mutations,
            mutation_root=_workspace_mutation_root(cwd, context)
            if workspace_mutations
            else cwd.resolve(),
            root_label="workspace",
            path_key="path",
        )
    )
    return tuple(mutation_plan)


def _mutation_entries(
    raw_mutations: list[object],
    mutation_root: Path,
    root_label: Literal["output", "workspace"],
    path_key: str,
) -> tuple[FixtureMutation, ...]:
    mutation_plan: list[FixtureMutation] = []
    for index, raw_mutation in enumerate(raw_mutations):
        if not isinstance(raw_mutation, dict):
            raise RuntimeError(
                "mock invoker mutation entries must be objects; "
                f"entry[{index}] is {type(raw_mutation).__name__}"
            )
        raw_path = raw_mutation.get(path_key)
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
                f"mock invoker {root_label} mutation path must stay within the "
                f"invocation {root_label} directory: {raw_path}"
            )
        mutation_plan.append(FixtureMutation(target=target, content=content))
    return tuple(mutation_plan)


def _entry_list(value: object, key: str) -> list[object]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RuntimeError(f"mock invoker mutation fixture key '{key}' must be a list")
    return list(value)


def _string_list(value: object, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError(f"mock invoker mutation fixture key '{key}' must be a list")
    return tuple(value)


def _workspace_mutation_root(cwd: Path, context: InvocationContext | None) -> Path:
    if context is None or context.workspace is None:
        raise RuntimeError(
            "mock invoker workspace mutations require a workspace invocation context"
        )
    checkout_root = context.workspace.checkout_root
    if checkout_root is None:
        raise RuntimeError("mock invoker workspace mutations require a checkout root")
    cwd_root = cwd.resolve()
    checkout = checkout_root.resolve()
    if not cwd_root.is_relative_to(checkout):
        raise RuntimeError("mock invoker workspace mutation cwd escapes checkout root")
    return cwd_root


def _validate_prompt_requirements(
    sidecar: JsonObject,
    prompt: str,
) -> None:
    for required in _string_list(
        sidecar.get("required_prompt_contains"),
        "required_prompt_contains",
    ):
        if required not in prompt:
            raise RuntimeError(
                "mock invoker prompt did not contain required fixture text"
            )
    for forbidden in _string_list(
        sidecar.get("forbidden_prompt_contains"),
        "forbidden_prompt_contains",
    ):
        if forbidden in prompt:
            raise RuntimeError("mock invoker prompt contained forbidden fixture text")

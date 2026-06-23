from __future__ import annotations

from pathlib import Path

from ..markdown.models import WorkflowImportConfig
from .models import CompositionResult, ImportSpec, ParamBinding


def import_specs_from_frontmatter(
    import_configs: list[WorkflowImportConfig],
    source_path: Path,
) -> tuple[ImportSpec, ...]:
    imports: list[ImportSpec] = []
    seen_aliases: set[str] = set()

    for import_config in import_configs:
        import_spec = ImportSpec(
            alias=import_config.alias,
            raw_path=import_config.path,
            with_params=dict(import_config.with_params),
            inputs=dict(import_config.input_bindings),
            source_path=source_path,
        )
        if import_spec.alias in seen_aliases:
            raise ValueError(
                f"Workflow '{source_path}' declares duplicate import alias "
                f"'{import_spec.alias}'."
            )
        seen_aliases.add(import_spec.alias)
        imports.append(import_spec)

    return tuple(imports)


def resolve_import_path(parent_file: Path, raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = parent_file.parent / candidate
    return candidate.resolve(strict=False)


def bind_import_params(
    inherited_params: dict[str, ParamBinding],
    child_namespace: str,
    import_params: dict[str, str],
) -> dict[str, ParamBinding]:
    effective_params = dict(inherited_params)
    for key, value in import_params.items():
        effective_params[key] = ParamBinding(
            value=value,
            binding_id=parameter_binding_id(child_namespace, key),
        )
    return effective_params


def validate_import_params_consumed(
    import_spec: ImportSpec,
    child_namespace: str,
    child_result: CompositionResult,
) -> None:
    unused_keys = sorted(
        key
        for key in import_spec.with_params
        if parameter_binding_id(child_namespace, key)
        not in child_result.consumed_param_bindings
    )
    if not unused_keys:
        return
    raise ValueError(
        "Workflow import "
        f"'{import_spec.alias}' in '{import_spec.source_path}' defines unused "
        f"parameter(s): {', '.join(unused_keys)}"
    )


def parameter_binding_id(namespace_prefix: str, key: str) -> str:
    if not namespace_prefix:
        return key
    return f"{namespace_prefix}:{key}"

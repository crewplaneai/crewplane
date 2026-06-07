from importlib import import_module
from typing import Any

_RUNTIME_EXPORTS = {
    "check_consensus": (".execution", "check_consensus"),
    "execute_parallel_stage": (".execution", "execute_parallel_stage"),
    "execute_sequential_stage": (".execution", "execute_sequential_stage"),
    "execute_workflow": (".execution", "execute_workflow"),
}

__all__ = [
    "check_consensus",
    "execute_parallel_stage",
    "execute_sequential_stage",
    "execute_workflow",
]


def __getattr__(name: str) -> Any:
    export = _RUNTIME_EXPORTS.get(name)
    if export is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attribute_name = export
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value

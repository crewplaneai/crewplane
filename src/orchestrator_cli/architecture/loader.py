from __future__ import annotations

import importlib
import inspect
from typing import Literal, NamedTuple, cast, overload

from .errors import AdapterContractError, AdapterLoadError, IntegrationResolutionError
from .ports import (
    ArtifactAdapterPort,
    InvokerAdapterPort,
    UIAdapterCapabilities,
    UIAdapterPort,
)
from .registry import INTEGRATION_ALIAS_REGISTRY, allowed_implementations

IntegrationKind = Literal["invoker", "ui", "artifacts"]
AdapterInstance = InvokerAdapterPort | UIAdapterPort | ArtifactAdapterPort


class ObjectPath(NamedTuple):
    module_name: str
    object_name: str


REQUIRED_FACTORY_METHOD: dict[IntegrationKind, str] = {
    "invoker": "create_invoker",
    "ui": "create_runtime",
    "artifacts": "create_store",
}
ADDITIONAL_REQUIRED_FACTORY_METHODS: dict[IntegrationKind, tuple[str, ...]] = {
    "invoker": ("canonicalize_options",),
    "ui": ("canonicalize_options",),
    "artifacts": ("canonicalize_options", "workflow_signature_exists"),
}


def _parse_object_path(path: str) -> ObjectPath:
    if ":" in path:
        module_name, object_name = path.split(":", 1)
        if not module_name or not object_name:
            raise AdapterLoadError(
                f"Invalid implementation path '{path}'. Use 'package.module:ClassName'."
            )
        return ObjectPath(module_name=module_name, object_name=object_name)

    if "." not in path:
        raise AdapterLoadError(
            f"Invalid implementation path '{path}'. "
            "Use 'package.module:ClassName' or 'package.module.ClassName'."
        )

    module_name, object_name = path.rsplit(".", 1)
    if not module_name or not object_name:
        raise AdapterLoadError(
            f"Invalid implementation path '{path}'. Use 'package.module.ClassName'."
        )
    return ObjectPath(module_name=module_name, object_name=object_name)


def resolve_implementation_path(
    integration_kind: IntegrationKind, implementation: str
) -> str:
    """Resolve an integration alias or dotted object path to a loadable target.

    Accepted implementations are registered aliases for the requested integration
    kind or direct object paths in ``package.module:ClassName`` or
    ``package.module.ClassName`` form. Unknown integration kinds or unregistered
    bare names raise ``IntegrationResolutionError``.
    """

    alias_map = INTEGRATION_ALIAS_REGISTRY.get(integration_kind)
    if alias_map is None:
        raise IntegrationResolutionError(
            f"Unknown integration kind '{integration_kind}'. "
            f"Supported kinds: {', '.join(sorted(INTEGRATION_ALIAS_REGISTRY))}"
        )

    if implementation in alias_map:
        return alias_map[implementation]

    if ":" in implementation or "." in implementation:
        return implementation

    allowed = ", ".join(allowed_implementations(integration_kind))
    raise IntegrationResolutionError(
        f"Unknown {integration_kind} implementation '{implementation}'. "
        f"Allowed aliases: {allowed}. "
        "You may also provide a dotted path."
    )


@overload
def load_adapter_class(
    integration_kind: Literal["artifacts"],
    implementation: str,
) -> type[ArtifactAdapterPort]: ...


@overload
def load_adapter_class(
    integration_kind: Literal["invoker"],
    implementation: str,
) -> type[InvokerAdapterPort]: ...


@overload
def load_adapter_class(
    integration_kind: Literal["ui"],
    implementation: str,
) -> type[UIAdapterPort]: ...


def load_adapter_class(
    integration_kind: IntegrationKind,
    implementation: str,
) -> type[AdapterInstance]:
    """Load and validate the adapter class for an integration implementation.

    ``implementation`` follows ``resolve_implementation_path`` alias and dotted
    path rules. Import failures or invalid object paths raise ``AdapterLoadError``;
    classes that do not expose the required factory method for the integration
    kind raise ``AdapterContractError``.
    """

    target_path = resolve_implementation_path(integration_kind, implementation)
    object_path = _parse_object_path(target_path)

    try:
        module = importlib.import_module(object_path.module_name)
    except Exception as exc:
        raise AdapterLoadError(
            f"Failed to import module '{object_path.module_name}' for "
            f"{integration_kind} implementation '{implementation}': {exc}"
        ) from exc

    if not hasattr(module, object_path.object_name):
        raise AdapterLoadError(
            f"Module '{object_path.module_name}' does not define "
            f"'{object_path.object_name}' for "
            f"{integration_kind} implementation '{implementation}'."
        )

    adapter_class = getattr(module, object_path.object_name)
    if not inspect.isclass(adapter_class):
        raise AdapterLoadError(
            f"Loaded object '{target_path}' is not a class for "
            f"{integration_kind} implementation '{implementation}'."
        )

    for required_method in (
        REQUIRED_FACTORY_METHOD[integration_kind],
        *ADDITIONAL_REQUIRED_FACTORY_METHODS[integration_kind],
    ):
        factory = getattr(adapter_class, required_method, None)
        if not callable(factory):
            raise AdapterContractError(
                f"Adapter class '{target_path}' must define '{required_method}()' "
                f"for {integration_kind} integration."
            )

    if integration_kind == "ui":
        capabilities = getattr(adapter_class, "capabilities", None)
        if not isinstance(capabilities, UIAdapterCapabilities):
            raise AdapterContractError(
                f"Adapter class '{target_path}' must define a "
                "UIAdapterCapabilities 'capabilities' attribute for ui integration."
            )

    return cast(type[AdapterInstance], adapter_class)


@overload
def instantiate_adapter(
    integration_kind: Literal["artifacts"],
    implementation: str,
) -> ArtifactAdapterPort: ...


@overload
def instantiate_adapter(
    integration_kind: Literal["invoker"],
    implementation: str,
) -> InvokerAdapterPort: ...


@overload
def instantiate_adapter(
    integration_kind: Literal["ui"],
    implementation: str,
) -> UIAdapterPort: ...


def instantiate_adapter(
    integration_kind: IntegrationKind,
    implementation: str,
) -> AdapterInstance:
    """Instantiate an adapter selected by alias or dotted class path.

    The path formats and alias behavior match ``load_adapter_class``. Constructor
    failures are wrapped in ``AdapterLoadError``; resolution and contract failures
    propagate as ``IntegrationResolutionError`` or ``AdapterContractError``.
    """

    adapter_class = load_adapter_class(integration_kind, implementation)
    try:
        return adapter_class()
    except Exception as exc:
        raise AdapterLoadError(
            f"Failed to instantiate adapter '{adapter_class.__module__}."
            f"{adapter_class.__name__}' for {integration_kind} implementation "
            f"'{implementation}': {exc}"
        ) from exc

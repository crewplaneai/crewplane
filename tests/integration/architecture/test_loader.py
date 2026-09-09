import unittest

import pytest

from crewplane.architecture.errors import (
    AdapterContractError,
    AdapterLoadError,
    IntegrationResolutionError,
)
from crewplane.architecture.loader import (
    instantiate_adapter,
    is_builtin_implementation,
    load_adapter_class,
    resolve_implementation_path,
)


class UIWithoutCapabilities:
    def canonicalize_options(self):  # type: ignore[no-untyped-def]
        return None

    def create_runtime(self):  # type: ignore[no-untyped-def]
        return None


class ArtifactsWithoutCanonicalOptions:
    def create_store(self):  # type: ignore[no-untyped-def]
        return None

    def create_terminal_history_reader(self):  # type: ignore[no-untyped-def]
        return None


class ArtifactsWithoutTerminalHistoryReader:
    def canonicalize_options(self):  # type: ignore[no-untyped-def]
        return None

    def create_store(self):  # type: ignore[no-untyped-def]
        return None


class InvokerWithStaticFactories:
    @staticmethod
    def canonicalize_options(
        implementation: str,
        resolved_identity: str,
        options: object | None = None,
    ) -> tuple[object, ...]:
        return implementation, resolved_identity, options

    @staticmethod
    def create_invoker(
        config: object,
        options: object | None = None,
    ) -> tuple[object, ...]:
        return config, options


class InvokerWithClassFactories:
    @classmethod
    def canonicalize_options(
        cls,
        implementation: str,
        resolved_identity: str,
        options: object | None = None,
    ) -> tuple[object, ...]:
        return cls, implementation, resolved_identity, options

    @classmethod
    def create_invoker(
        cls,
        config: object,
        options: object | None = None,
    ) -> tuple[object, ...]:
        return cls, config, options


class LoaderTests(unittest.TestCase):
    def test_resolve_alias_to_builtin_path(self) -> None:
        resolved = resolve_implementation_path("invoker", "cli")
        self.assertEqual(
            resolved,
            "crewplane.adapters.invokers.cli:CliInvokerAdapter",
        )

    def test_resolve_mock_alias_to_builtin_path(self) -> None:
        resolved = resolve_implementation_path("invoker", "mock")
        self.assertEqual(
            resolved,
            "crewplane.adapters.invokers.mock:MockInvokerAdapter",
        )

    def test_resolve_supports_dotted_override(self) -> None:
        resolved = resolve_implementation_path(
            "ui",
            "crewplane.adapters.ui.null:NullUIAdapter",
        )
        self.assertEqual(
            resolved,
            "crewplane.adapters.ui.null:NullUIAdapter",
        )

    def test_unknown_alias_raises_with_allowed_values(self) -> None:
        with self.assertRaisesRegex(IntegrationResolutionError, "Allowed aliases"):
            resolve_implementation_path("ui", "missing")

    def test_unknown_integration_kind_raises(self) -> None:
        with self.assertRaisesRegex(
            IntegrationResolutionError, "Unknown integration kind"
        ):
            resolve_implementation_path("missing", "cli")

    def test_load_class_supports_colon_path(self) -> None:
        cls = load_adapter_class(
            "invoker",
            "crewplane.adapters.invokers.cli:CliInvokerAdapter",
        )
        self.assertEqual(cls.__name__, "CliInvokerAdapter")

    def test_load_class_supports_dot_path(self) -> None:
        cls = load_adapter_class(
            "ui",
            "crewplane.adapters.ui.null.NullUIAdapter",
        )
        self.assertEqual(cls.__name__, "NullUIAdapter")

    def test_load_invoker_class_accepts_static_and_class_factories(self) -> None:
        for class_name, expected_prefix in (
            ("InvokerWithStaticFactories", ()),
            ("InvokerWithClassFactories", (InvokerWithClassFactories,)),
        ):
            with self.subTest(class_name=class_name):
                cls = load_adapter_class("invoker", f"{__name__}.{class_name}")
                adapter = cls()

                self.assertEqual(
                    adapter.canonicalize_options("alias", "resolved", {"key": 1}),
                    (*expected_prefix, "alias", "resolved", {"key": 1}),
                )
                self.assertEqual(
                    adapter.create_invoker("config", {"key": 1}),
                    (*expected_prefix, "config", {"key": 1}),
                )

    def test_contract_violation_raises_for_wrong_class(self) -> None:
        with self.assertRaisesRegex(AdapterContractError, "create_invoker"):
            load_adapter_class(
                "invoker",
                "crewplane.adapters.ui.null:NullUIAdapter",
            )

    def test_ui_contract_requires_capabilities(self) -> None:
        with self.assertRaisesRegex(AdapterContractError, "capabilities"):
            load_adapter_class(
                "ui",
                f"{__name__}:UIWithoutCapabilities",
            )

    def test_artifact_contract_requires_canonical_options(self) -> None:
        with self.assertRaisesRegex(AdapterContractError, "canonicalize_options"):
            load_adapter_class(
                "artifacts",
                f"{__name__}:ArtifactsWithoutCanonicalOptions",
            )

    def test_artifact_contract_requires_terminal_history_reader(self) -> None:
        with self.assertRaisesRegex(
            AdapterContractError,
            "create_terminal_history_reader",
        ):
            load_adapter_class(
                "artifacts",
                f"{__name__}:ArtifactsWithoutTerminalHistoryReader",
            )

    def test_invalid_colon_path_raises_clear_error(self) -> None:
        with self.assertRaisesRegex(AdapterLoadError, "Invalid implementation path"):
            load_adapter_class("ui", "crewplane.adapters.ui.tmux:")

    def test_non_class_object_path_raises(self) -> None:
        with self.assertRaisesRegex(AdapterLoadError, "is not a class"):
            load_adapter_class(
                "ui",
                "crewplane.architecture.registry:INTEGRATION_ALIAS_REGISTRY",
            )

    def test_instantiate_adapter_returns_instance(self) -> None:
        adapter = instantiate_adapter("artifacts", "filesystem")
        self.assertEqual(adapter.__class__.__name__, "FilesystemArtifactsAdapter")

    def test_instantiate_mock_invoker_adapter_returns_instance(self) -> None:
        adapter = instantiate_adapter("invoker", "mock")
        self.assertEqual(adapter.__class__.__name__, "MockInvokerAdapter")


@pytest.mark.parametrize(
    "implementation",
    ["", ":", ".", "unknown", "module:", ":Class", "module.", ".Class"],
)
def test_builtin_identity_probe_rejects_invalid_targets_without_loading(
    implementation, monkeypatch
) -> None:
    import crewplane.architecture.loader as loader

    def unexpected_import(name: str):
        raise AssertionError(f"identity probe imported {name}")

    monkeypatch.setattr(loader.importlib, "import_module", unexpected_import)
    assert not is_builtin_implementation("invoker", implementation, "cli")


def test_builtin_identity_probe_uses_registry_targets(monkeypatch) -> None:
    import crewplane.architecture.loader as loader

    registry = dict(loader.INTEGRATION_ALIAS_REGISTRY)
    registry["invoker"] = {**registry["invoker"], "mock": "example.custom:Adapter"}
    monkeypatch.setattr(loader, "INTEGRATION_ALIAS_REGISTRY", registry)
    assert is_builtin_implementation("invoker", "mock", "mock")
    assert is_builtin_implementation("invoker", "example.custom.Adapter", "mock")
    assert not is_builtin_implementation(
        "invoker", "crewplane.adapters.invokers.mock:MockInvokerAdapter", "mock"
    )

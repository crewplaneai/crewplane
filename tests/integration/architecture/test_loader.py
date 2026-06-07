import unittest

from orchestrator_cli.architecture.errors import (
    AdapterContractError,
    AdapterLoadError,
    IntegrationResolutionError,
)
from orchestrator_cli.architecture.loader import (
    instantiate_adapter,
    load_adapter_class,
    resolve_implementation_path,
)


class UIWithoutCapabilities:
    def canonicalize_options(self):  # type: ignore[no-untyped-def]
        return None

    def create_runtime(self):  # type: ignore[no-untyped-def]
        return None


class ArtifactsWithoutSignatureLookup:
    def canonicalize_options(self):  # type: ignore[no-untyped-def]
        return None

    def create_store(self):  # type: ignore[no-untyped-def]
        return None


class LoaderTests(unittest.TestCase):
    def test_resolve_alias_to_builtin_path(self) -> None:
        resolved = resolve_implementation_path("invoker", "cli")
        self.assertEqual(
            resolved,
            "orchestrator_cli.adapters.invokers.cli:CliInvokerAdapter",
        )

    def test_resolve_mock_alias_to_builtin_path(self) -> None:
        resolved = resolve_implementation_path("invoker", "mock")
        self.assertEqual(
            resolved,
            "orchestrator_cli.adapters.invokers.mock:MockInvokerAdapter",
        )

    def test_resolve_supports_dotted_override(self) -> None:
        resolved = resolve_implementation_path(
            "ui",
            "orchestrator_cli.adapters.ui.null:NullUIAdapter",
        )
        self.assertEqual(
            resolved,
            "orchestrator_cli.adapters.ui.null:NullUIAdapter",
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
            "orchestrator_cli.adapters.invokers.cli:CliInvokerAdapter",
        )
        self.assertEqual(cls.__name__, "CliInvokerAdapter")

    def test_load_class_supports_dot_path(self) -> None:
        cls = load_adapter_class(
            "ui",
            "orchestrator_cli.adapters.ui.null.NullUIAdapter",
        )
        self.assertEqual(cls.__name__, "NullUIAdapter")

    def test_contract_violation_raises_for_wrong_class(self) -> None:
        with self.assertRaisesRegex(AdapterContractError, "create_invoker"):
            load_adapter_class(
                "invoker",
                "orchestrator_cli.adapters.ui.null:NullUIAdapter",
            )

    def test_ui_contract_requires_capabilities(self) -> None:
        with self.assertRaisesRegex(AdapterContractError, "capabilities"):
            load_adapter_class(
                "ui",
                f"{__name__}:UIWithoutCapabilities",
            )

    def test_artifact_contract_requires_signature_lookup(self) -> None:
        with self.assertRaisesRegex(AdapterContractError, "workflow_signature_exists"):
            load_adapter_class(
                "artifacts",
                f"{__name__}:ArtifactsWithoutSignatureLookup",
            )

    def test_invalid_colon_path_raises_clear_error(self) -> None:
        with self.assertRaisesRegex(AdapterLoadError, "Invalid implementation path"):
            load_adapter_class("ui", "orchestrator_cli.adapters.ui.tmux:")

    def test_non_class_object_path_raises(self) -> None:
        with self.assertRaisesRegex(AdapterLoadError, "is not a class"):
            load_adapter_class(
                "ui",
                "orchestrator_cli.architecture.registry:INTEGRATION_ALIAS_REGISTRY",
            )

    def test_instantiate_adapter_returns_instance(self) -> None:
        adapter = instantiate_adapter("artifacts", "filesystem")
        self.assertEqual(adapter.__class__.__name__, "FilesystemArtifactsAdapter")

    def test_instantiate_mock_invoker_adapter_returns_instance(self) -> None:
        adapter = instantiate_adapter("invoker", "mock")
        self.assertEqual(adapter.__class__.__name__, "MockInvokerAdapter")

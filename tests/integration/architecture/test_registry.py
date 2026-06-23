import unittest

from crewplane.architecture.registry import (
    INTEGRATION_ALIAS_REGISTRY,
    allowed_implementations,
)


class RegistryTests(unittest.TestCase):
    def test_registry_contains_expected_builtin_aliases(self) -> None:
        self.assertIn("cli", INTEGRATION_ALIAS_REGISTRY["invoker"])
        self.assertIn("mock", INTEGRATION_ALIAS_REGISTRY["invoker"])
        self.assertIn("tmux", INTEGRATION_ALIAS_REGISTRY["ui"])
        self.assertIn("none", INTEGRATION_ALIAS_REGISTRY["ui"])
        self.assertIn("filesystem", INTEGRATION_ALIAS_REGISTRY["artifacts"])

    def test_allowed_implementations_returns_sorted_aliases(self) -> None:
        self.assertEqual(allowed_implementations("invoker"), ["cli", "mock"])
        self.assertEqual(allowed_implementations("ui"), ["none", "tmux"])
        self.assertEqual(allowed_implementations("artifacts"), ["filesystem"])

    def test_allowed_implementations_unknown_kind_returns_empty(self) -> None:
        self.assertEqual(allowed_implementations("missing"), [])

    def test_registry_is_immutable(self) -> None:
        with self.assertRaises(TypeError):
            INTEGRATION_ALIAS_REGISTRY["invoker"]["custom"] = "pkg:Adapter"  # type: ignore[index]

import json
from pathlib import Path
from tempfile import mkdtemp

import pytest

from crewplane.adapters.invokers.mock import MockInvokerAdapter
from crewplane.adapters.invokers.mock_invoker.options import parse_options
from crewplane.architecture.contracts import InvocationContext, MockInvokerOptions
from crewplane.core.config import AgentConfig
from crewplane.core.workflow.keywords import ProviderRole
from tests.integration.adapters.mock_invoker.mock_invoker_test_case import (
    MockInvokerAdapterTestCase,
)


@pytest.mark.parametrize("options", [None, {}])
def test_omitted_options_use_dto_defaults_and_keep_canonical_shape(options):
    defaults = MockInvokerOptions()

    assert parse_options(options) == defaults
    canonical = MockInvokerAdapter().canonicalize_options("mock", "mock", options)

    assert canonical.options == {
        "delay_seconds": defaults.delay_seconds,
        "fail_when": [],
        "observation_delay_seconds": defaults.observation_delay_seconds,
        "output_dir": defaults.output_dir,
        "output_mode": defaults.output_mode,
        "seed": defaults.seed,
        "strict_file_mode": defaults.strict_file_mode,
    }
    assert "fixture_metadata" not in canonical.options


def test_explicit_zero_options_override_dto_defaults():
    parsed = parse_options(
        {"delay_seconds": 0, "observation_delay_seconds": 0, "seed": 0}
    )

    assert parsed.delay_seconds == 0
    assert parsed.observation_delay_seconds == 0
    assert parsed.seed == 0


class SeedInteger(int):
    pass


@pytest.mark.parametrize("seed", [None, -1, 0, 1, 2**128, SeedInteger(7)])
def test_optional_seed_preserves_integer_values_and_identity(seed):
    assert parse_options({"seed": seed}).seed is seed


@pytest.mark.parametrize("seed", [True, False, 1.5, "7"])
def test_optional_seed_rejects_non_integers_with_existing_message(seed):
    with pytest.raises(ValueError) as caught:
        parse_options({"seed": seed})

    assert str(caught.value) == "mock invoker option 'seed' must be an integer or null"


class MockInvokerOptionsAndLoggingTests(MockInvokerAdapterTestCase):
    @pytest.fixture(autouse=True)
    def temporary_directory_root(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    async def test_seeded_lorem_output_is_deterministic(self) -> None:
        adapter = MockInvokerAdapter()
        invoker = adapter.create_invoker(
            config=self._build_config(),
            options=self._options(seed=42, output_mode="lorem"),
        )
        tmp_dir = mkdtemp(dir=self.tmp_path)
        root = Path(tmp_dir)
        output_a = root / "a.md"
        output_b = root / "b.md"
        context = InvocationContext(
            node_id="node.a",
            task_id="alpha_executor_0",
            provider="alpha",
            role=ProviderRole.EXECUTOR,
            round_num=1,
        )
        await invoker.invoke(
            config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
            model="model-a",
            prompt="same prompt",
            output_file=output_a,
            cwd=(output_a).parent,
            invocation_context=context,
        )
        await invoker.invoke(
            config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
            model="model-a",
            prompt="same prompt",
            output_file=output_b,
            cwd=(output_b).parent,
            invocation_context=context,
        )
        self.assertEqual(
            output_a.read_text(encoding="utf-8"),
            output_b.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            output_a.read_text(encoding="utf-8"),
            "# Mock Invocation Output\n"
            "\n"
            "- Node: node.a\n"
            "- Task: alpha_executor_0\n"
            "- Provider: alpha\n"
            "- Role: executor\n"
            "- Audit Round: n/a\n"
            "- Round: 1\n"
            "- Seed Marker: a95392e8b671\n"
            "\n"
            "## Summary\n"
            "Synthetic output generated for deterministic local "
            "orchestration checks.\n"
            "\n"
            "## Notes\n"
            "- Prompt length: 11 characters\n"
            "- Behavior path: mock invoker lorem mode\n"
            "\n"
            "## Next Steps\n"
            "1. Verify downstream template substitution.\n"
            "2. Validate node and invocation state transitions.\n",
        )

    def test_canonical_selector_serialization_preserves_field_order(self) -> None:
        canonical = MockInvokerAdapter().canonicalize_options(
            "mock",
            "crewplane.adapters.invokers.mock.MockInvokerAdapter",
            self._options(
                fail_when=[
                    {
                        "node_id": "node.a",
                        "task_id": "alpha_reviewer_0",
                        "provider": "alpha",
                        "role": "reviewer",
                        "audit_round_num": 2,
                        "round_num": 3,
                    }
                ]
            ),
        )

        self.assertEqual(
            json.dumps(canonical.options["fail_when"], separators=(",", ":")),
            '[{"node_id":"node.a","task_id":"alpha_reviewer_0",'
            '"provider":"alpha","role":"reviewer","audit_round_num":2,'
            '"round_num":3}]',
        )

    def test_create_invoker_rejects_unknown_options(self) -> None:
        adapter = MockInvokerAdapter()
        with self.assertRaisesRegex(ValueError, "Unsupported mock invoker options"):
            adapter.create_invoker(
                config=self._build_config(),
                options=self._options(mystery=1),
            )

    async def test_canonical_failure_selector_preserves_matching(self) -> None:
        adapter = MockInvokerAdapter()
        canonical = adapter.canonicalize_options(
            "mock",
            "crewplane.adapters.invokers.mock:MockInvokerAdapter",
            self._options(fail_when=[{"node_id": "node.a"}]),
        )
        invoker = adapter.create_invoker(self._build_config(), canonical.options)
        output = self.tmp_path / "failed.md"

        with pytest.raises(
            RuntimeError, match="forced failure by selector: node_id=node.a"
        ):
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"]),
                model=None,
                prompt="hello",
                output_file=output,
                cwd=self.tmp_path,
                invocation_context=self._context(),
            )
        assert not output.exists()

        await invoker.invoke(
            config=AgentConfig(cli_cmd=["echo"]),
            model=None,
            prompt="hello",
            output_file=output,
            cwd=self.tmp_path,
            invocation_context=InvocationContext(
                node_id="other",
                task_id="other",
                provider="alpha",
                role=ProviderRole.EXECUTOR,
            ),
        )
        assert output.is_file()

    def test_authored_failure_selector_still_rejects_null_criteria(self) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            MockInvokerAdapter().canonicalize_options(
                "mock",
                "crewplane.adapters.invokers.mock:MockInvokerAdapter",
                self._options(fail_when=[{"node_id": "node.a", "provider": None}]),
            )

    def test_create_invoker_exposes_mock_log_presentation_descriptor(self) -> None:
        adapter = MockInvokerAdapter()
        invoker = adapter.create_invoker(
            config=self._build_config(),
            options=self._options(),
        )

        descriptor = invoker.log_presentation_for(
            AgentConfig(cli_cmd=["echo"], default_model="model-a")
        )

        assert descriptor is not None
        self.assertEqual(
            (descriptor.format, descriptor.profile), ("json_lines", "mock")
        )

    def test_workspace_capabilities_declare_mock_no_child_process(self) -> None:
        adapter = MockInvokerAdapter()

        capabilities = adapter.canonicalize_options(
            "mock",
            "crewplane.adapters.invokers.mock:MockInvokerAdapter",
            self._options(),
        ).capabilities["workspace"]

        self.assertEqual(capabilities["supported"], True)
        self.assertEqual(capabilities["launch_mode"], "mock_no_child_process")
        self.assertEqual(capabilities["honors_cwd"], True)
        self.assertEqual(capabilities["controlled_child_environment"], False)

    def test_create_invoker_rejects_invalid_option_types_and_values(self) -> None:
        adapter = MockInvokerAdapter()
        invalid_options = [
            self._options(delay_seconds=-1),
            self._options(delay_seconds=True),
            self._options(delay_seconds=float("nan")),
            self._options(delay_seconds=float("inf")),
            self._options(observation_delay_seconds=-1),
            self._options(observation_delay_seconds=True),
            self._options(observation_delay_seconds=float("nan")),
            self._options(observation_delay_seconds=float("inf")),
            self._options(output_mode="invalid"),
            self._options(output_mode=1),
            self._options(output_mode="file"),
            self._options(output_mode="file", output_dir=""),
            self._options(strict_file_mode="yes"),
            self._options(seed=True),
            self._options(seed=1.5),
            self._options(fail_when={}),
            self._options(fail_when=[{}]),
            self._options(fail_when=[{"unsupported": "x"}]),
            self._options(fail_when=[{"node_id": 1}]),
        ]
        for options in invalid_options:
            with self.subTest(options=options), self.assertRaises(ValueError):
                adapter.create_invoker(
                    config=self._build_config(),
                    options=options,
                )

    def test_create_invoker_rejects_non_string_option_keys(self) -> None:
        adapter = MockInvokerAdapter()
        with self.assertRaisesRegex(ValueError, "keys must be strings"):
            adapter.create_invoker(config=self._build_config(), options={1: "bad"})  # type: ignore[arg-type]

    def test_create_invoker_rejects_non_string_fail_selector_keys(self) -> None:
        adapter = MockInvokerAdapter()
        with self.assertRaisesRegex(ValueError, "selector keys must be strings"):
            adapter.create_invoker(
                config=self._build_config(),
                options=self._options(fail_when=[{1: "bad"}]),  # type: ignore[list-item]
            )

    async def test_log_file_appends_structured_summary(self) -> None:
        adapter = MockInvokerAdapter()
        tmp_dir = mkdtemp(dir=self.tmp_path)
        root = Path(tmp_dir)
        fixture = root / "fixtures" / "default.md"
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text("fixture-default", encoding="utf-8")
        invoker = adapter.create_invoker(
            config=self._build_config(),
            options=self._options(
                output_mode="file",
                output_dir=str(root / "fixtures"),
            ),
        )
        output_file = root / "out.md"
        log_file = root / "logs" / "mock.log"
        await invoker.invoke(
            config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
            model="model-a",
            prompt="x",
            output_file=output_file,
            cwd=(output_file).parent,
            log_file=log_file,
            invocation_context=InvocationContext(
                node_id="node.a",
                task_id="alpha_executor_0",
                provider="alpha",
                role=ProviderRole.EXECUTOR,
                round_num=1,
            ),
        )

        lines = log_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["invoker"], "mock")
        self.assertEqual(record["output_mode"], "file")
        self.assertEqual(record["source"], "fixture")
        self.assertEqual(record["node_id"], "node.a")
        self.assertIsNone(record["audit_round_num"])

    async def test_log_file_records_audit_round_num_when_present(self) -> None:
        adapter = MockInvokerAdapter()
        tmp_dir = mkdtemp(dir=self.tmp_path)
        root = Path(tmp_dir)
        fixture = root / "fixtures" / "default.md"
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text("fixture-default", encoding="utf-8")
        invoker = adapter.create_invoker(
            config=self._build_config(),
            options=self._options(
                output_mode="file",
                output_dir=str(root / "fixtures"),
            ),
        )
        output_file = root / "out.md"
        log_file = root / "logs" / "mock.log"

        await invoker.invoke(
            config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
            model="model-a",
            prompt="x",
            output_file=output_file,
            cwd=(output_file).parent,
            log_file=log_file,
            invocation_context=self._context(audit_round_num=3, round_num=1),
        )

        record = json.loads(log_file.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(record["audit_round_num"], 3)

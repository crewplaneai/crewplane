import tempfile
import time
from pathlib import Path

from orchestrator_cli.adapters.invokers.mock import MockInvokerAdapter
from orchestrator_cli.architecture.contracts import InvocationContext
from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.runtime.execution.consensus import (
    VERDICT_NO_FINDINGS,
    ParsedReviewResult,
    parse_review_result,
    render_review_contract,
)
from tests.integration.adapters.mock_invoker.mock_invoker_test_case import (
    MockInvokerAdapterTestCase,
)


class MockInvokerOutputModeTests(MockInvokerAdapterTestCase):
    async def test_default_options_write_lorem_output(self) -> None:
        adapter = MockInvokerAdapter()
        invoker = adapter.create_invoker(
            config=self._build_config(),
            options=self._options(),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "out.md"
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="hello world",
                output_file=output_file,
                invocation_context=self._context(),
            )
            text = output_file.read_text(encoding="utf-8")

        self.assertIn("# Mock Invocation Output", text)
        self.assertIn("- Node: node.a", text)
        self.assertIn("## Summary", text)

    async def test_default_options_accept_none_model(self) -> None:
        adapter = MockInvokerAdapter()
        invoker = adapter.create_invoker(
            config=self._build_config(),
            options=self._options(),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "out.md"
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"]),
                model=None,
                prompt="hello world",
                output_file=output_file,
                invocation_context=self._context(),
            )
            text = output_file.read_text(encoding="utf-8")

        self.assertIn("# Mock Invocation Output", text)

    async def test_echo_mode_writes_prompt_exactly(self) -> None:
        adapter = MockInvokerAdapter()
        invoker = adapter.create_invoker(
            config=self._build_config(),
            options=self._options(output_mode="echo"),
        )
        prompt = "exact prompt\nline two"
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "echo.md"
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt=prompt,
                output_file=output_file,
                invocation_context=InvocationContext(
                    node_id="node.echo",
                    task_id="alpha_executor_0",
                    provider="alpha",
                    role="executor",
                    round_num=1,
                    findings_enabled=True,
                ),
            )
            self.assertEqual(output_file.read_text(encoding="utf-8"), prompt)

    async def test_echo_mode_writes_structured_review_contract_for_reviewer(
        self,
    ) -> None:
        adapter = MockInvokerAdapter()
        invoker = adapter.create_invoker(
            config=self._build_config(),
            options=self._options(output_mode="echo"),
        )
        prompt = "review this output"
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "review.md"
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt=prompt,
                output_file=output_file,
                invocation_context=self._context(role="reviewer"),
            )
            text = output_file.read_text(encoding="utf-8")

        self.assertNotEqual(text, prompt)
        self.assertEqual(parse_review_result(text).verdict, VERDICT_NO_FINDINGS)
        self.assertEqual(
            text,
            render_review_contract(
                ParsedReviewResult(
                    verdict=VERDICT_NO_FINDINGS,
                    major_issues="None",
                    minor_issues="None",
                    nitpicks="None",
                )
            ),
        )

    async def test_lorem_mode_adds_findings_block_when_enabled(self) -> None:
        adapter = MockInvokerAdapter()
        invoker = adapter.create_invoker(
            config=self._build_config(),
            options=self._options(output_mode="lorem", seed=42),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "out.md"
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="review the repository",
                output_file=output_file,
                invocation_context=InvocationContext(
                    node_id="review.context",
                    task_id="alpha_executor_0",
                    provider="alpha",
                    role="executor",
                    round_num=1,
                    findings_enabled=True,
                ),
            )

            text = output_file.read_text(encoding="utf-8")

        self.assertEqual(text.count("<!-- findings -->"), 1)
        self.assertIn("<!-- /findings -->", text)
        self.assertIn("Synthetic finding for review.context", text)

    async def test_lorem_mode_writes_structured_review_contract_for_reviewer(
        self,
    ) -> None:
        adapter = MockInvokerAdapter()
        invoker = adapter.create_invoker(
            config=self._build_config(),
            options=self._options(output_mode="lorem", seed=42),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "review.md"
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="review the repository",
                output_file=output_file,
                invocation_context=self._context(role="reviewer"),
            )
            text = output_file.read_text(encoding="utf-8")

        self.assertEqual(parse_review_result(text).verdict, VERDICT_NO_FINDINGS)
        self.assertNotIn("# Mock Invocation Output", text)
        self.assertEqual(
            text,
            render_review_contract(
                ParsedReviewResult(
                    verdict=VERDICT_NO_FINDINGS,
                    major_issues="None",
                    minor_issues="None",
                    nitpicks="None",
                )
            ),
        )

    async def test_delay_seconds_enforces_minimum_elapsed_duration(self) -> None:
        adapter = MockInvokerAdapter()
        invoker = adapter.create_invoker(
            config=self._build_config(),
            options=self._options(delay_seconds=0.05),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "delay.md"
            started_at = time.perf_counter()
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="delay",
                output_file=output_file,
            )
            elapsed = time.perf_counter() - started_at
        self.assertGreaterEqual(elapsed, 0.045)

    async def test_fail_when_matches_single_selector(self) -> None:
        adapter = MockInvokerAdapter()
        invoker = adapter.create_invoker(
            config=self._build_config(),
            options=self._options(fail_when=[{"node_id": "node.fail"}]),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "out.md"
            with self.assertRaisesRegex(RuntimeError, "forced failure"):
                await invoker.invoke(
                    config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                    model="model-a",
                    prompt="x",
                    output_file=output_file,
                    invocation_context=InvocationContext(
                        node_id="node.fail",
                        task_id="alpha_executor_0",
                        provider="alpha",
                        role="executor",
                        round_num=1,
                    ),
                )

    async def test_fail_when_matches_composite_selector(self) -> None:
        adapter = MockInvokerAdapter()
        invoker = adapter.create_invoker(
            config=self._build_config(),
            options=self._options(
                fail_when=[
                    {
                        "node_id": "summary.final",
                        "provider": "alpha",
                        "role": "reviewer",
                        "round_num": 2,
                    }
                ]
            ),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "out.md"
            with self.assertRaisesRegex(RuntimeError, "forced failure"):
                await invoker.invoke(
                    config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                    model="model-a",
                    prompt="x",
                    output_file=output_file,
                    invocation_context=InvocationContext(
                        node_id="summary.final",
                        task_id="alpha_reviewer_0",
                        provider="alpha",
                        role="reviewer",
                        round_num=2,
                    ),
                )

    async def test_fail_when_matches_audit_round_selector(self) -> None:
        adapter = MockInvokerAdapter()
        invoker = adapter.create_invoker(
            config=self._build_config(),
            options=self._options(
                fail_when=[
                    {
                        "node_id": "review.node",
                        "audit_round_num": 2,
                        "round_num": 1,
                    }
                ]
            ),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "out.md"
            with self.assertRaisesRegex(RuntimeError, "forced failure"):
                await invoker.invoke(
                    config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                    model="model-a",
                    prompt="x",
                    output_file=output_file,
                    invocation_context=InvocationContext(
                        node_id="review.node",
                        task_id="alpha_reviewer_0",
                        provider="alpha",
                        role="reviewer",
                        audit_round_num=2,
                        round_num=1,
                    ),
                )

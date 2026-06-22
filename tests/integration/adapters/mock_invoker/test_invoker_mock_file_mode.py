import json
import tempfile
from pathlib import Path

from orchestrator_cli.adapters.invokers.mock import MockInvokerAdapter
from orchestrator_cli.architecture.contracts import (
    InvocationContext,
    InvocationSourceContext,
    InvocationWorkspaceContext,
    InvocationWorktreeContract,
)
from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.runtime.execution.consensus import (
    VERDICT_NO_FINDINGS,
    parse_review_result,
)
from orchestrator_cli.version import SCHEMA_VERSION
from tests.integration.adapters.mock_invoker.mock_invoker_test_case import (
    MockInvokerAdapterTestCase,
)


class MockInvokerFileModeTests(MockInvokerAdapterTestCase):
    async def test_file_mode_reads_exact_fixture(self) -> None:
        adapter = MockInvokerAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fixture = root / "fixtures" / "node.a" / "alpha_executor_0_round2.md"
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text("fixture-round2", encoding="utf-8")

            invoker = adapter.create_invoker(
                config=self._build_config(),
                options=self._options(
                    output_mode="file",
                    output_dir=str(root / "fixtures"),
                ),
            )
            output_file = root / "out.md"
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="x",
                output_file=output_file,
                cwd=(output_file).parent,
                invocation_context=InvocationContext(
                    node_id="node.a",
                    task_id="alpha_executor_0",
                    provider="alpha",
                    role="executor",
                    round_num=2,
                ),
            )
            self.assertEqual(output_file.read_text(encoding="utf-8"), "fixture-round2")

    async def test_file_mode_reads_grouped_audit_round_fixture_first(self) -> None:
        adapter = MockInvokerAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fixture = (
                root
                / "fixtures"
                / "node.a"
                / "review-audit-round-2"
                / "alpha_executor_0_round1.md"
            )
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text("grouped-audit-fixture", encoding="utf-8")
            invoker = adapter.create_invoker(
                config=self._build_config(),
                options=self._options(
                    output_mode="file",
                    output_dir=str(root / "fixtures"),
                ),
            )
            output_file = root / "out.md"
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="x",
                output_file=output_file,
                cwd=(output_file).parent,
                invocation_context=self._context(
                    audit_round_num=2,
                    round_num=1,
                ),
            )

            self.assertEqual(
                output_file.read_text(encoding="utf-8"),
                "grouped-audit-fixture",
            )

    async def test_file_mode_applies_fixture_mutation_sidecars(self) -> None:
        adapter = MockInvokerAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fixture = root / "fixtures" / "node.a" / "alpha_executor_0_round1.md"
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text("fixture-round1", encoding="utf-8")
            fixture.with_suffix(".mutations.json").write_text(
                json.dumps(
                    [
                        {
                            "path": "review-state/mutated-note.md",
                            "content": "mutation applied",
                        }
                    ],
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            invoker = adapter.create_invoker(
                config=self._build_config(),
                options=self._options(
                    output_mode="file",
                    output_dir=str(root / "fixtures"),
                ),
            )
            output_file = root / "out.md"
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="x",
                output_file=output_file,
                cwd=(output_file).parent,
                invocation_context=self._context(round_num=1),
            )

            self.assertEqual(output_file.read_text(encoding="utf-8"), "fixture-round1")
            self.assertEqual(
                (output_file.parent / "review-state" / "mutated-note.md").read_text(
                    encoding="utf-8"
                ),
                "mutation applied",
            )

    async def test_file_mode_rejects_mutation_paths_outside_fixture_directory(
        self,
    ) -> None:
        adapter = MockInvokerAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fixture = root / "fixtures" / "node.a" / "alpha_executor_0_round1.md"
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text("fixture-round1", encoding="utf-8")
            fixture.with_suffix(".mutations.json").write_text(
                json.dumps(
                    [{"path": "../escape.md", "content": "bad"}],
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            invoker = adapter.create_invoker(
                config=self._build_config(),
                options=self._options(
                    output_mode="file",
                    output_dir=str(root / "fixtures"),
                ),
            )
            output_file = root / "out.md"
            with self.assertRaisesRegex(
                RuntimeError,
                "must stay within the invocation output directory",
            ):
                await invoker.invoke(
                    config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                    model="model-a",
                    prompt="x",
                    output_file=output_file,
                    cwd=(output_file).parent,
                    invocation_context=self._context(round_num=1),
                )
            self.assertFalse(output_file.exists())

    async def test_file_mode_applies_workspace_mutation_sidecars(self) -> None:
        adapter = MockInvokerAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workspace = root / "workspace"
            workspace.mkdir()
            fixture = root / "fixtures" / "node.a" / "alpha_executor_0_round1.md"
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text("fixture-round1", encoding="utf-8")
            fixture.with_suffix(".mutations.json").write_text(
                json.dumps(
                    {
                        "forbidden_prompt_contains": ["stale source"],
                        "required_prompt_contains": ["candidate source"],
                        "workspace_mutations": [
                            {
                                "path": "src/app.txt",
                                "content": "workspace mutation applied",
                            }
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            invoker = adapter.create_invoker(
                config=self._build_config(),
                options=self._options(
                    output_mode="file",
                    output_dir=str(root / "fixtures"),
                ),
            )
            output_file = root / "out.md"
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="candidate source",
                output_file=output_file,
                cwd=workspace,
                invocation_context=self._workspace_context(workspace),
            )

            self.assertEqual(
                (workspace / "src" / "app.txt").read_text(encoding="utf-8"),
                "workspace mutation applied",
            )

    async def test_file_mode_rejects_workspace_mutation_without_workspace_context(
        self,
    ) -> None:
        adapter = MockInvokerAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fixture = root / "fixtures" / "node.a" / "alpha_executor_0_round1.md"
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text("fixture-round1", encoding="utf-8")
            fixture.with_suffix(".mutations.json").write_text(
                json.dumps(
                    {
                        "workspace_mutations": [
                            {"path": "src/app.txt", "content": "bad"}
                        ]
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            invoker = adapter.create_invoker(
                config=self._build_config(),
                options=self._options(
                    output_mode="file",
                    output_dir=str(root / "fixtures"),
                ),
            )
            output_file = root / "out.md"
            with self.assertRaisesRegex(
                RuntimeError,
                "workspace mutations require a workspace invocation context",
            ):
                await invoker.invoke(
                    config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                    model="model-a",
                    prompt="candidate source",
                    output_file=output_file,
                    cwd=root,
                    invocation_context=self._context(round_num=1),
                )

    async def test_file_mode_rejects_failed_prompt_sidecar_requirements(
        self,
    ) -> None:
        adapter = MockInvokerAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fixture = root / "fixtures" / "node.a" / "alpha_executor_0_round1.md"
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text("fixture-round1", encoding="utf-8")
            fixture.with_suffix(".mutations.json").write_text(
                json.dumps(
                    {"required_prompt_contains": ["candidate source"]},
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            invoker = adapter.create_invoker(
                config=self._build_config(),
                options=self._options(
                    output_mode="file",
                    output_dir=str(root / "fixtures"),
                ),
            )
            output_file = root / "out.md"
            with self.assertRaisesRegex(
                RuntimeError,
                "prompt did not contain required fixture text",
            ):
                await invoker.invoke(
                    config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                    model="model-a",
                    prompt="stale source",
                    output_file=output_file,
                    cwd=root,
                    invocation_context=self._context(round_num=1),
                )

    async def test_file_mode_fallback_priority(self) -> None:
        adapter = MockInvokerAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fixtures_dir = root / "fixtures"
            node_dir = fixtures_dir / "node.a"
            node_dir.mkdir(parents=True, exist_ok=True)
            invoker = adapter.create_invoker(
                config=self._build_config(),
                options=self._options(
                    output_mode="file",
                    output_dir=str(fixtures_dir),
                ),
            )
            output_file = root / "out.md"
            context = InvocationContext(
                node_id="node.a",
                task_id="alpha_executor_0",
                provider="alpha",
                role="executor",
                round_num=3,
            )

            (node_dir / "executor-round-3.md").write_text(
                "role-round-level", encoding="utf-8"
            )
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="x",
                output_file=output_file,
                cwd=(output_file).parent,
                invocation_context=context,
            )
            self.assertEqual(
                output_file.read_text(encoding="utf-8"), "role-round-level"
            )

            (node_dir / "executor-round-3.md").unlink()
            (node_dir / "alpha_executor_0.md").write_text(
                "task-level", encoding="utf-8"
            )
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="x",
                output_file=output_file,
                cwd=(output_file).parent,
                invocation_context=context,
            )
            self.assertEqual(output_file.read_text(encoding="utf-8"), "task-level")

            (node_dir / "alpha_executor_0.md").unlink()
            (node_dir / "executor.md").write_text("role-level", encoding="utf-8")
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="x",
                output_file=output_file,
                cwd=(output_file).parent,
                invocation_context=context,
            )
            self.assertEqual(output_file.read_text(encoding="utf-8"), "role-level")

            (node_dir / "executor.md").unlink()
            (fixtures_dir / "node.a.md").write_text("node-level", encoding="utf-8")
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="x",
                output_file=output_file,
                cwd=(output_file).parent,
                invocation_context=context,
            )
            self.assertEqual(output_file.read_text(encoding="utf-8"), "node-level")

            (fixtures_dir / "node.a.md").unlink()
            (fixtures_dir / "default-executor.md").write_text(
                "default-role-level", encoding="utf-8"
            )
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="x",
                output_file=output_file,
                cwd=(output_file).parent,
                invocation_context=context,
            )
            self.assertEqual(
                output_file.read_text(encoding="utf-8"), "default-role-level"
            )

            (fixtures_dir / "default-executor.md").unlink()
            (fixtures_dir / "default.md").write_text("default-level", encoding="utf-8")
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="x",
                output_file=output_file,
                cwd=(output_file).parent,
                invocation_context=context,
            )
            self.assertEqual(output_file.read_text(encoding="utf-8"), "default-level")

    async def test_file_mode_flat_lookup_does_not_use_node_local_role_default(
        self,
    ) -> None:
        adapter = MockInvokerAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fixtures_dir = root / "fixtures"
            node_dir = fixtures_dir / "node.a"
            node_dir.mkdir(parents=True, exist_ok=True)
            (node_dir / "default-executor.md").write_text(
                "node-local-role-default",
                encoding="utf-8",
            )
            (fixtures_dir / "node.a.md").write_text("node-level", encoding="utf-8")
            invoker = adapter.create_invoker(
                config=self._build_config(),
                options=self._options(
                    output_mode="file",
                    output_dir=str(fixtures_dir),
                ),
            )
            output_file = root / "out.md"

            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="x",
                output_file=output_file,
                cwd=(output_file).parent,
                invocation_context=self._context(role="executor", round_num=4),
            )

            self.assertEqual(output_file.read_text(encoding="utf-8"), "node-level")

    async def test_file_mode_reviewer_role_defaults_take_precedence(self) -> None:
        adapter = MockInvokerAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fixtures_dir = root / "fixtures"
            fixtures_dir.mkdir(parents=True, exist_ok=True)
            (fixtures_dir / "default-reviewer.md").write_text(
                "reviewer-role-default",
                encoding="utf-8",
            )
            (fixtures_dir / "default.md").write_text("global-default", encoding="utf-8")
            invoker = adapter.create_invoker(
                config=self._build_config(),
                options=self._options(
                    output_mode="file",
                    output_dir=str(fixtures_dir),
                ),
            )
            output_file = root / "out.md"

            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="review",
                output_file=output_file,
                cwd=(output_file).parent,
                invocation_context=self._context(role="reviewer", round_num=4),
            )

            self.assertEqual(
                output_file.read_text(encoding="utf-8"), "reviewer-role-default"
            )

    async def test_file_mode_strict_fails_without_fixture(self) -> None:
        adapter = MockInvokerAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            invoker = adapter.create_invoker(
                config=self._build_config(),
                options={
                    "output_mode": "file",
                    "output_dir": str(root / "fixtures"),
                    "strict_file_mode": True,
                    "observation_delay_seconds": 0,
                },
            )
            output_file = root / "out.md"
            with self.assertRaisesRegex(RuntimeError, "could not resolve fixture"):
                await invoker.invoke(
                    config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                    model="model-a",
                    prompt="x",
                    output_file=output_file,
                    cwd=(output_file).parent,
                    invocation_context=InvocationContext(
                        node_id="node.a",
                        task_id="alpha_executor_0",
                        provider="alpha",
                        role="executor",
                        round_num=1,
                    ),
                )

    async def test_file_mode_falls_back_to_lorem_when_not_strict(self) -> None:
        adapter = MockInvokerAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            invoker = adapter.create_invoker(
                config=self._build_config(),
                options={
                    "output_mode": "file",
                    "output_dir": str(root / "fixtures"),
                    "strict_file_mode": False,
                    "seed": 99,
                    "observation_delay_seconds": 0,
                },
            )
            output_file = root / "out.md"
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="x",
                output_file=output_file,
                cwd=(output_file).parent,
                invocation_context=InvocationContext(
                    node_id="node.a",
                    task_id="alpha_executor_0",
                    provider="alpha",
                    role="executor",
                    round_num=1,
                ),
            )

            text = output_file.read_text(encoding="utf-8")
            self.assertIn("# Mock Invocation Output", text)
            self.assertIn("- Seed Marker:", text)

    async def test_file_mode_fallback_lorem_adds_findings_block_when_enabled(
        self,
    ) -> None:
        adapter = MockInvokerAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            invoker = adapter.create_invoker(
                config=self._build_config(),
                options={
                    "output_mode": "file",
                    "output_dir": str(root / "fixtures"),
                    "strict_file_mode": False,
                    "observation_delay_seconds": 0,
                },
            )
            output_file = root / "out.md"
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="review",
                output_file=output_file,
                cwd=(output_file).parent,
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
        self.assertIn("Synthetic finding for review.context", text)

    async def test_file_mode_uses_reviewer_fixture_exactly_when_present(self) -> None:
        adapter = MockInvokerAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fixture = root / "fixtures" / "node.a" / "alpha_reviewer_0_round2.md"
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text("fixture-review", encoding="utf-8")
            invoker = adapter.create_invoker(
                config=self._build_config(),
                options=self._options(
                    output_mode="file",
                    output_dir=str(root / "fixtures"),
                ),
            )
            output_file = root / "out.md"
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="review",
                output_file=output_file,
                cwd=(output_file).parent,
                invocation_context=self._context(
                    role="reviewer",
                    task_id="alpha_reviewer_0",
                    round_num=2,
                ),
            )

            self.assertEqual(output_file.read_text(encoding="utf-8"), "fixture-review")

    async def test_file_mode_falls_back_to_review_contract_for_reviewer(self) -> None:
        adapter = MockInvokerAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            invoker = adapter.create_invoker(
                config=self._build_config(),
                options={
                    "output_mode": "file",
                    "output_dir": str(root / "fixtures"),
                    "strict_file_mode": False,
                    "observation_delay_seconds": 0,
                },
            )
            output_file = root / "out.md"
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="review",
                output_file=output_file,
                cwd=(output_file).parent,
                invocation_context=self._context(role="reviewer"),
            )

            self.assertEqual(
                parse_review_result(output_file.read_text(encoding="utf-8")).verdict,
                VERDICT_NO_FINDINGS,
            )

    async def test_file_mode_fixture_does_not_rewrite_findings_enabled_content(
        self,
    ) -> None:
        adapter = MockInvokerAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fixture = root / "fixtures" / "default.md"
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text("fixture-content", encoding="utf-8")
            invoker = adapter.create_invoker(
                config=self._build_config(),
                options=self._options(
                    output_mode="file",
                    output_dir=str(root / "fixtures"),
                ),
            )
            output_file = root / "out.md"
            await invoker.invoke(
                config=AgentConfig(cli_cmd=["echo"], default_model="model-a"),
                model="model-a",
                prompt="review",
                output_file=output_file,
                cwd=(output_file).parent,
                invocation_context=InvocationContext(
                    node_id="review.context",
                    task_id="alpha_executor_0",
                    provider="alpha",
                    role="executor",
                    round_num=1,
                    findings_enabled=True,
                ),
            )

            self.assertEqual(output_file.read_text(encoding="utf-8"), "fixture-content")

    def _workspace_context(self, workspace: Path) -> InvocationContext:
        return InvocationContext(
            node_id="node.a",
            task_id="alpha_executor_0",
            provider="alpha",
            role="executor",
            round_num=1,
            workspace=InvocationWorkspaceContext(
                workspace_kind="worktree",
                materialization="worktree_checkout",
                logical_worktree_name="primary",
                cwd=workspace,
                invocation_source=InvocationSourceContext(
                    source_kind="project",
                    source_node_id=None,
                    source_commit="abc123",
                    source_tree="def456",
                ),
                worktree_contract=InvocationWorktreeContract(
                    mode="blob_exact",
                    schema_version=SCHEMA_VERSION,
                ),
                checkout_root=workspace,
                writable=True,
                lineage_producer=True,
            ),
        )

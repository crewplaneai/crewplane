import io
import unittest

from rich.console import Console

from orchestrator_cli.adapters.ui.null import NullUIAdapter
from orchestrator_cli.core.config import Config
from orchestrator_cli.observability.types import WorkflowTopology
from orchestrator_cli.versions import CONFIG_SCHEMA_VERSION


class NullUIAdapterTests(unittest.TestCase):
    def test_create_runtime_returns_empty_plan(self) -> None:
        adapter = NullUIAdapter()
        runtime = adapter.create_runtime(
            config=Config(version=CONFIG_SCHEMA_VERSION, agents={}),
            workflow_topology=WorkflowTopology(workflow_name="w", nodes=()),
            run_id="run",
            console=Console(file=io.StringIO()),
            options={},
        )
        self.assertEqual(runtime.observers, ())
        self.assertFalse(runtime.suppress_progress_output)

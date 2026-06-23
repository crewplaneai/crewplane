import io
import unittest

from rich.console import Console

from crewplane.adapters.ui.null import NullUIAdapter
from crewplane.core.config import Config
from crewplane.observability.types import WorkflowTopology
from crewplane.version import SCHEMA_VERSION


class NullUIAdapterTests(unittest.TestCase):
    def test_create_runtime_returns_empty_plan(self) -> None:
        adapter = NullUIAdapter()
        runtime = adapter.create_runtime(
            config=Config(version=SCHEMA_VERSION, agents={}),
            workflow_topology=WorkflowTopology(workflow_name="w", nodes=()),
            run_id="run",
            console=Console(file=io.StringIO()),
            options={},
        )
        self.assertEqual(runtime.observers, ())
        self.assertFalse(runtime.suppress_progress_output)

import io
from unittest.mock import Mock

import pytest
from rich.console import Console

from crewplane.adapters.ui.tmux import TmuxUIAdapter
from crewplane.cli.cleanup import resolve_cleanup_workspace_context
from crewplane.cli.update.runner import update_crewplane
from crewplane.cli.update.types import UpdateError
from crewplane.core.config import Config
from crewplane.observability.types import WorkflowTopology
from crewplane.version import SCHEMA_VERSION


@pytest.fixture(autouse=True)
def windows(monkeypatch):
    monkeypatch.setattr("crewplane.core.platform.platform.system", lambda: "Windows")


def test_windows_cleanup_rejects_before_context_resolution(
    monkeypatch, tmp_path
) -> None:
    resolve = Mock(side_effect=AssertionError("cleanup must not inspect project"))
    monkeypatch.setattr(
        "crewplane.cli.workspace_cleanup.context.resolve_cleanup_workspace_context",
        resolve,
    )
    with pytest.raises(ValueError, match="maintenance is not supported"):
        resolve_cleanup_workspace_context(
            Console(file=io.StringIO()),
            tmp_path / "missing.yml",
            False,
            False,
            False,
            False,
            None,
            None,
            False,
        )
    resolve.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_windows_self_update_rejects_before_package_manager(monkeypatch) -> None:
    context = Mock(side_effect=AssertionError("update must not resolve an installer"))
    monkeypatch.setattr("crewplane.cli.update.runner.default_update_context", context)
    with pytest.raises(UpdateError, match="pip or uv"):
        update_crewplane()
    context.assert_not_called()


def test_windows_tmux_keeps_console_progress_without_discovery() -> None:
    discover = Mock(side_effect=AssertionError("tmux must not be discovered"))
    plan = TmuxUIAdapter().create_runtime(
        Config(version=SCHEMA_VERSION, agents={}),
        WorkflowTopology(workflow_name="w", nodes=()),
        "run",
        Console(file=io.StringIO()),
        which_fn=discover,
    )
    assert plan.observers == ()
    assert plan.suppress_progress_output is False
    discover.assert_not_called()

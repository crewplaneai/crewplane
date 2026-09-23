from functools import partial
from pathlib import Path

import pytest

import crewplane.artifacts.locks as resume_locks
from crewplane.artifacts.locks import acquire_same_context_lock
from crewplane.artifacts.naming import build_provider_process_state_filename
from crewplane.core.execution_state import RUN_STATE_SCHEMA_VERSION
from crewplane.core.provider_process_state import ProviderProcessState
from tests.helpers.resume_locks import FakeProcessInspector, UnsafeProcessInspector
from tests.integration.cli.repeat_force_run_support import (
    create_project,
    filesystem_snapshot,
)


@pytest.mark.parametrize("protection", ["active", "unsafe_owner", "live_provider"])
@pytest.mark.parametrize("force", [False, True])
def test_repetition_preserves_existing_lock_protections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protection: str, force: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    project = create_project(tmp_path, None, node_count=1)
    assert project.run("--no-live").exit_code == 0
    manifest = project.manifests()[0]
    state_dir = tmp_path / ".crewplane"
    lock = acquire_same_context_lock(
        state_dir,
        "Repeat",
        manifest["workflow_identity"],
        manifest["workflow_signature"],
        process_inspector=FakeProcessInspector(100, "old"),
    )
    lock.update_run(manifest["run_id"], manifest["run_key_name"])
    if protection == "active":
        inspector = partial(FakeProcessInspector, 200, "new", live=True)
    elif protection == "unsafe_owner":
        inspector = partial(UnsafeProcessInspector, 200, "new")
    else:
        inspector = partial(FakeProcessInspector, 200, "new", live_checks=[False, True])
        state = ProviderProcessState(
            run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
            run_id=manifest["run_id"],
            run_key_name=manifest["run_key_name"],
            node_id="node0",
            task_id="alpha_executor_0",
            provider="alpha",
            role="executor",
            round_num=1,
            attempt=1,
            pid=300,
            process_group_id=300,
            hostname="host",
            process_start_identity="provider-start",
            status="started",
            started_at=manifest["started_at"],
        )
        filename = build_provider_process_state_filename(
            state.node_id, state.task_id, state.provider, state.role, None, 1, 1
        )
        process_dir = (
            state_dir
            / "execution-stages"
            / manifest["run_key_name"]
            / "manifests/provider-processes"
        )
        process_dir.mkdir()
        (process_dir / filename).write_text(
            state.model_dump_json(exclude_none=True), encoding="utf-8"
        )
    monkeypatch.setattr(resume_locks, "ProcessInspector", inspector)
    project.set_count(3)
    before = filesystem_snapshot(state_dir)
    try:
        result = project.run("--no-live", *(["--force"] if force else []))
        assert result.exit_code == 1, result.output
        assert "Run lock unavailable" in result.output
        assert "Run 2" not in result.output
        assert filesystem_snapshot(state_dir) == before
    finally:
        lock.release()

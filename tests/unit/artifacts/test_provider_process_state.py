from __future__ import annotations

import json
import os
from datetime import datetime

import pytest

from crewplane.architecture.contracts import InvocationProcessEvent
from crewplane.architecture.ports import ProviderProcessInvocation
from crewplane.artifacts.manager import OutputManager
from crewplane.core.provider_process_state import ProviderProcessState


def _invocation(
    *,
    task_id: str = "codex_executor_0",
    round_num: int = 1,
) -> ProviderProcessInvocation:
    return ProviderProcessInvocation(
        node_id="build.node",
        task_id=task_id,
        provider="codex",
        role="executor",
        audit_round_num=None,
        round_num=round_num,
    )


def test_provider_process_state_records_start_and_exit(tmp_path) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    invocation = _invocation()
    started = InvocationProcessEvent(
        attempt=1,
        pid=os.getpid(),
        process_group_id=os.getpgrp(),
        status="started",
    )

    state_path = output.write_provider_process_event(invocation, started).path

    started_state = ProviderProcessState.model_validate_json(
        state_path.read_text(encoding="utf-8")
    )
    assert state_path.parent.name == "provider-processes"
    assert started_state.status == "started"
    assert started_state.run_id == output.run_id
    assert started_state.node_id == invocation.node_id
    assert started_state.attempt == 1
    assert started_state.pid == os.getpid()
    assert started_state.exited_at is None

    exited_path = output.write_provider_process_event(
        invocation,
        InvocationProcessEvent(
            attempt=1,
            pid=os.getpid(),
            process_group_id=os.getpgrp(),
            status="exited",
            returncode=0,
        ),
    ).path

    exited_state = ProviderProcessState.model_validate_json(
        exited_path.read_text(encoding="utf-8")
    )
    assert exited_path == state_path
    assert exited_state.status == "exited"
    assert exited_state.returncode == 0
    assert exited_state.exited_at is not None


def test_provider_process_attempts_use_distinct_state_files(tmp_path) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    invocation = _invocation()

    first_path = output.write_provider_process_event(
        invocation,
        InvocationProcessEvent(
            attempt=1,
            pid=os.getpid(),
            process_group_id=None,
            status="started",
        ),
    ).path
    second_path = output.write_provider_process_event(
        invocation,
        InvocationProcessEvent(
            attempt=2,
            pid=os.getpid(),
            process_group_id=None,
            status="started",
        ),
    ).path

    assert first_path != second_path
    assert len(first_path.name) <= 180
    assert len(second_path.name) <= 180


def test_provider_process_state_accepts_initial_reviewer_round_zero(tmp_path) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)

    state_path = output.write_provider_process_event(
        _invocation(task_id="codex_reviewer_0", round_num=0),
        InvocationProcessEvent(
            attempt=1,
            pid=os.getpid(),
            process_group_id=None,
            status="started",
        ),
    ).path

    state = ProviderProcessState.model_validate_json(
        state_path.read_text(encoding="utf-8")
    )
    assert state.round_num == 0


def test_provider_process_exit_must_match_started_process(tmp_path) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    invocation = _invocation()
    output.write_provider_process_event(
        invocation,
        InvocationProcessEvent(
            attempt=1,
            pid=os.getpid(),
            process_group_id=None,
            status="started",
        ),
    )

    with pytest.raises(RuntimeError, match="does not match"):
        output.write_provider_process_event(
            invocation,
            InvocationProcessEvent(
                attempt=1,
                pid=os.getpid() + 1,
                process_group_id=None,
                status="exited",
                returncode=0,
            ),
        )


def test_provider_process_exit_rejects_modified_started_state(tmp_path) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    invocation = _invocation()
    state_path = output.write_provider_process_event(
        invocation,
        InvocationProcessEvent(
            attempt=1,
            pid=os.getpid(),
            process_group_id=None,
            status="started",
        ),
    ).path
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload.update(
        status="exited",
        exited_at=datetime.now().isoformat(),
        returncode=0,
    )
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed unexpectedly"):
        output.write_provider_process_event(
            invocation,
            InvocationProcessEvent(
                attempt=1,
                pid=os.getpid(),
                process_group_id=None,
                status="exited",
                returncode=0,
            ),
        )


def test_provider_process_state_contains_no_command_or_environment(tmp_path) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    state_path = output.write_provider_process_event(
        _invocation(),
        InvocationProcessEvent(
            attempt=1,
            pid=os.getpid(),
            process_group_id=None,
            status="started",
        ),
    ).path

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert "command" not in payload
    assert "environment" not in payload
    assert "prompt" not in payload

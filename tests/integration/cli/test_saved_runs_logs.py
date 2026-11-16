from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from crewplane.cli.app import app
from crewplane.cli.runs import streaming
from crewplane.observability.events import EventType
from tests.helpers.saved_runs import saved_event, saved_run, write_events


def test_raw_invalid_event_content_is_not_decoded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    record = saved_run(tmp_path / ".crewplane")
    path = write_events(record, [])
    text = "[red]not JSON[/red]\nsecond line\r\n\x1b[0m"
    path.write_bytes(text.encode())
    result = CliRunner().invoke(app, ["runs", "logs", "latest"], color=True)
    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == text.encode()
    assert f"Log: {path}" in result.stderr


def test_exact_node_log_stdout_and_selection_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    record = saved_run(tmp_path / ".crewplane")
    path = record.run_dir / "provider.log"
    path.write_bytes(b"[red]literal brackets[/red]\n")
    write_events(
        record,
        [
            saved_event(
                record,
                EventType.INVOCATION_STARTED,
                node_id="import.each[3].review",
                task_id="[red]task",
                provider="mock",
                audit_round_num=2,
                round_num=0,
                log_file="provider.log",
            )
        ],
    )
    result = CliRunner().invoke(
        app, ["runs", "logs", "latest", "--node", "import.each[3].review"]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == "[red]literal brackets[/red]\n"
    assert (
        "Node: import.each[3].review; task: [red]task; provider: mock; audit-round: 2; round: 0"
        in result.stderr
    )
    assert str(path) in result.stderr


def test_reused_log_is_labeled_as_source_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    state = tmp_path / ".crewplane"
    source = saved_run(state, "old", "workflow--old")
    (source.run_dir / "provider.log").write_bytes(b"source evidence")
    write_events(
        source,
        [
            saved_event(
                source,
                EventType.INVOCATION_STARTED,
                node_id="n",
                task_id="t",
                provider="p",
                round_num=1,
                log_file="provider.log",
            )
        ],
    )
    saved_run(
        state,
        "new",
        "workflow--new",
        started_offset=1,
        resumed_nodes=["n"],
        resume_source_run_id="old",
        resume_source_run_key_name="workflow--old",
    )
    result = CliRunner().invoke(app, ["runs", "logs", "latest", "--node", "n"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "source evidence"
    assert "Requested run: workflow--new" in result.stderr
    assert "Source evidence run: workflow--old" in result.stderr


def test_excluded_history_warning_never_contaminates_raw_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    state = tmp_path / ".crewplane"
    record = saved_run(state)
    path = write_events(record, [])
    path.write_bytes(b"one\n")
    corrupt = saved_run(state, "bad", "bad-key")
    corrupt.manifest_path.write_bytes(b"invalid JSON")
    result = CliRunner().invoke(app, ["runs", "logs", "latest"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "one\n"
    assert "Excluded 'bad-key'" in result.stderr


@pytest.mark.parametrize("follow", [False, True])
def test_closed_stdout_pipe_exits_without_shutdown_error(
    tmp_path: Path, follow: bool
) -> None:
    record = saved_run(tmp_path / ".crewplane")
    path = write_events(record, [])
    path.write_bytes(b"buffered log\n")
    command = [
        sys.executable,
        "-E",
        "-m",
        "crewplane.cli.app",
        "runs",
        "logs",
        "latest",
    ]
    if follow:
        command.append("--follow")
    reader, writer = os.pipe()
    os.close(reader)
    with os.fdopen(writer, "wb") as closed_pipe:
        result = subprocess.run(
            command,
            cwd=tmp_path,
            stdout=closed_pipe,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stderr.decode().splitlines() == [f"Log: {path}"]


@pytest.mark.race
def test_follow_pins_latest_and_invocation_until_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    state = tmp_path / ".crewplane"
    record = saved_run(state)
    path = record.run_dir / "provider.log"
    path.write_bytes(b"initial\n")
    events = [
        saved_event(
            record,
            EventType.INVOCATION_STARTED,
            node_id="n",
            task_id="t",
            provider="p",
            round_num=1,
            log_file="provider.log",
        )
    ]
    write_events(record, events)
    polls = 0

    def next_poll(seconds: float) -> None:
        nonlocal polls
        assert seconds > 0
        polls += 1
        if polls == 1:
            saved_run(state, "new", "new-key", started_offset=10)
            (record.run_dir / "later.log").write_bytes(b"never switch")
            write_events(
                record,
                [
                    *events,
                    saved_event(
                        record,
                        EventType.INVOCATION_STARTED,
                        node_id="n",
                        task_id="t",
                        provider="p",
                        round_num=2,
                        timestamp_utc="2026-06-09T12:00:02+00:00",
                        log_file="later.log",
                    ),
                ],
            )
            with path.open("ab") as output:
                output.write(b"append\n")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(streaming.time, "sleep", next_poll)
    result = CliRunner().invoke(
        app, ["runs", "logs", "latest", "--node", "n", "--follow"]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == "initial\nappend\n"
    assert "later.log" not in result.stderr
    assert polls == 2


@pytest.mark.parametrize("kind", ["missing", "unknown", "corrupt"])
def test_log_failure_is_nonzero_and_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.chdir(tmp_path)
    record = saved_run(tmp_path / ".crewplane")
    args = ["runs", "logs", "latest"]
    if kind != "missing":
        path = write_events(record, [saved_event(record)])
        args.extend(["--node", "n"])
        if kind == "corrupt":
            path.write_bytes(b"bad\n")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Saved run inspection failed" in result.stderr


@pytest.mark.parametrize(
    "reference,exit_code,stdout",
    [
        ({"path": "new.log"}, 1, ""),
        ("", 1, ""),
        ("   ", 1, ""),
        (None, 0, "old invocation\n"),
        ("new.log", 0, "new invocation\n"),
    ],
)
def test_invalid_log_reference_cannot_select_older_output(
    tmp_path, monkeypatch, reference, exit_code, stdout
):
    monkeypatch.chdir(tmp_path)
    record = saved_run(tmp_path / ".crewplane")
    for task in ("old", "new"):
        (record.run_dir / f"{task}.log").write_bytes(f"{task} invocation\n".encode())
    path = write_events(
        record,
        [
            saved_event(
                record,
                EventType.INVOCATION_STARTED,
                node_id="n",
                task_id=task,
                provider="p",
                round_num=index,
                log_file=f"{task}.log",
                timestamp_utc=f"2026-01-01T00:00:0{index}Z",
            )
            for index, task in enumerate(("old", "new"))
        ],
    )
    events = [json.loads(line) for line in path.read_bytes().splitlines()]
    events[-1]["log_file"] = reference
    content = "".join(json.dumps(event) + "\n" for event in events).encode()
    path.write_bytes(content)
    runner = CliRunner()
    result = runner.invoke(app, ["runs", "logs", "latest", "--node", "n"])
    assert result.exit_code == exit_code, result.output
    assert result.stdout == stdout
    if exit_code:
        assert "evidence incomplete" in result.stderr
    raw = runner.invoke(app, ["runs", "logs", "latest"])
    assert raw.exit_code == 0, raw.output
    assert raw.stdout_bytes == content

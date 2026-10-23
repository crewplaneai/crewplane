import argparse
import io
import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import pytest
from crewplane_dots_host import Queue, invoke, main


def receipt(model=None, reasoning=None):
    return {
        "output": "actual result",
        "host_agent_id": "native-agent-123",
        "evidence": "host dispatch receipt",
        "confirmed": {"model": model, "reasoning": reasoning},
    }


@pytest.fixture
def queue(tmp_path):
    instance = Queue(tmp_path)
    yield instance
    instance.close()


def test_completion_and_exact_replay(queue):
    job = queue.submit("review", "gpt-6.1-sol", "xhigh")
    claim = queue.claim(job, 20)
    queue.complete(job, claim["token"], receipt("gpt-6.1-sol", "xhigh"))
    queue.complete(job, claim["token"], receipt("gpt-6.1-sol", "xhigh"))
    assert queue.inspect(job)[0]["state"] == "completed"
    with pytest.raises(ValueError):
        queue.complete(job, claim["token"], {**receipt(), "output": "different"})


def test_claim_fencing_and_unknown(queue):
    job = queue.submit("review", None, None)
    claim = queue.claim(job, 20)
    for identifier in (job, "missing"):
        with pytest.raises(ValueError):
            queue.claim(identifier, 20)
    with pytest.raises(ValueError):
        queue.complete(job, "wrong-token", receipt())
    queue.cancel(job)
    with pytest.raises(ValueError):
        queue.complete(job, claim["token"], receipt())


def test_expired_lease_never_requeued(queue, monkeypatch):
    job = queue.submit("review", None, None)
    claim = queue.claim(job, 20)
    monkeypatch.setattr(time, "time", lambda: claim["lease"] + 1)
    with pytest.raises(ValueError):
        queue.renew(job, claim["token"], 20)
    with pytest.raises(ValueError):
        queue.complete(job, claim["token"], receipt())
    assert queue.reconcile() == 1
    assert queue.inspect(job)[0]["state"] == "expired"


def test_pending_deadline_blocks_dispatch(queue, monkeypatch):
    job = queue.submit("review", None, None, timeout=10)
    deadline = queue.inspect(job)[0]["request"]["expires_at"]
    monkeypatch.setattr(time, "time", lambda: deadline + 1)
    with pytest.raises(ValueError):
        queue.claim(job, 10)
    assert queue.inspect(job)[0]["state"] == "expired"


def test_explicit_model_does_not_fallback(queue):
    job = queue.submit("review", "requested-model", "xhigh")
    claim = queue.claim(job, 20)
    with pytest.raises(ValueError, match="not confirmed"):
        queue.complete(job, claim["token"], receipt())
    queue.renew(job, claim["token"], 30)
    queue.complete(job, claim["token"], receipt("requested-model", "xhigh"))


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf")])
def test_invalid_durations(queue, seconds):
    with pytest.raises(ValueError):
        queue.submit("x", None, None, seconds)


def test_subprocess_transport_and_cancellation(tmp_path):
    command = [
        sys.executable,
        "-m",
        "crewplane_dots_host",
        "--queue",
        str(tmp_path),
        "invoke",
        "--timeout",
        "20",
    ]
    for cancel in (False, True):
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        queue = Queue(tmp_path)
        try:
            process.stdin.write("real stdin prompt")
            process.stdin.close()
            # A ready receipt, not sleep, establishes the interleaving.
            ready = process.stderr.readline()
            job = ready.strip().split()[-1]
            assert queue.inspect(job)[0]["request"]["prompt"] == "real stdin prompt"
            claim = queue.claim(job, 20)
            if cancel:
                process.send_signal(signal.SIGTERM)
                assert process.wait(timeout=5) != 0
                assert queue.inspect(job)[0]["state"] == "cancelled"
                with pytest.raises(ValueError):
                    queue.complete(job, claim["token"], receipt())
            else:
                queue.complete(job, claim["token"], receipt())
                assert process.wait(timeout=5) == 0
                assert process.stdout.read().strip() == "actual result"
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
            queue.close()


@pytest.mark.parametrize("prompt", [b"", b"partial prompt"], ids=["empty", "partial"])
def test_invocation_timeout_bounds_open_stdin(tmp_path: Path, prompt: bytes) -> None:
    reader_fd, writer_fd = os.pipe()
    with os.fdopen(reader_fd, "rb") as reader, os.fdopen(writer_fd, "wb") as writer:
        writer.write(prompt)
        writer.flush()
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "crewplane_dots_host",
                "--queue",
                str(tmp_path),
                "invoke",
                "--timeout",
                "0.1",
            ],
            stdin=reader,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    assert process.returncode == 1
    assert stdout == ""
    assert "timed out while reading stdin" in stderr
    with closing(Queue(tmp_path)) as queue:
        assert queue.inspect() == []


def test_independent_connections_cannot_claim_twice(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    queue = Queue(tmp_path)
    try:
        job = queue.submit("one invocation", None, None)
        barrier = Barrier(2)

        def claim():
            contender = Queue(tmp_path)
            try:
                barrier.wait(timeout=5)
                try:
                    return contender.claim(job, 30)["token"]
                except ValueError:
                    return None
            finally:
                contender.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        assert sum(value is not None for value in results) == 1
    finally:
        queue.close()


@pytest.mark.parametrize("value", [{}, {"output": 3}, [], None])
def test_malformed_receipt_rejected(queue, value):
    job = queue.submit("review", None, None)
    claim = queue.claim(job, 20)
    with pytest.raises(ValueError):
        queue.complete(job, claim["token"], value)
    assert queue.inspect(job)[0]["state"] == "claimed"


def test_cli_host_commands(tmp_path):
    import json

    def run(*args):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "crewplane_dots_host",
                "--queue",
                str(tmp_path),
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )

    queue = Queue(tmp_path)
    try:
        job = queue.submit("review", None, None)
        assert json.loads(run("list").stdout)[0]["id"] == job
        claim = json.loads(run("claim", job).stdout)
        assert run("renew", job, "--token", claim["token"]).returncode == 0
        result = tmp_path / "result.json"
        result.write_text(json.dumps(receipt()))
        assert (
            run(
                "complete", job, "--token", claim["token"], "--receipt", str(result)
            ).returncode
            == 0
        )
        assert run("cancel", job).returncode == 0
        assert run("reconcile").stdout.strip() == "0"
        assert run("claim", "missing").returncode == 1
        assert (
            run(
                "complete", "missing", "--token", "missing", "--receipt", str(result)
            ).returncode
            == 1
        )
        assert run("invoke", "--timeout", "0.001").returncode == 1
    finally:
        queue.close()


@pytest.mark.parametrize(
    "change",
    [
        {"output": ""},
        {"host_agent_id": False},
        {"confirmed": {}},
        {"confirmed": {"model": 5, "reasoning": None}},
    ],
)
def test_receipt_field_types(queue, change):
    job = queue.submit("review", None, None)
    claim = queue.claim(job, 20)
    with pytest.raises(ValueError):
        queue.complete(job, claim["token"], {**receipt(), **change})


def test_external_cancellation_stops_invocation(tmp_path):
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "crewplane_dots_host",
            "--queue",
            str(tmp_path),
            "invoke",
            "--timeout",
            "10",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    queue = Queue(tmp_path)
    try:
        job = process.stderr.readline().strip().split()[-1]
        queue.cancel(job)
        assert process.wait(timeout=5) == 1
        assert "cancelled" in process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        queue.close()


@pytest.mark.parametrize("operation", ["claim", "renew"])
def test_write_lock_wait_cannot_revive_expiration(tmp_path, monkeypatch, operation):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    queue = Queue(tmp_path)
    job = queue.submit("x", None, None, 10)
    token = queue.claim(job, 5)["token"] if operation == "renew" else None
    ready, start, waiting = Event(), Event(), Event()

    def compete():
        contender = Queue(tmp_path)
        try:
            contender.db.execute("PRAGMA busy_timeout=1000")
            # Isolate eligibility SQL from the separate housekeeping transaction.
            if operation == "claim":
                contender.reconcile = lambda: 0
            contender.db.set_trace_callback(
                lambda statement: (
                    waiting.set() if statement.startswith(("BEGIN", "UPDATE")) else None
                )
            )
            ready.set()
            assert start.wait(5)
            with pytest.raises(ValueError):
                if operation == "claim":
                    contender.claim(job, 30)
                else:
                    contender.renew(job, token, 30)
        finally:
            contender.close()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(compete)
            assert ready.wait(5)
            queue.db.execute("BEGIN IMMEDIATE")
            start.set()
            assert waiting.wait(5)
            clock[0] = 1020.0
            queue.db.rollback()
            future.result(timeout=5)
    finally:
        queue.db.rollback()
        queue.close()


def test_core_termination_fences_receipt_despite_locked_database(tmp_path):
    from crewplane.runtime.agent.process.drain import drain_popen_process

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "crewplane_dots_host",
            "--queue",
            str(tmp_path),
            "invoke",
            "--timeout",
            "20",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    queue = Queue(tmp_path)
    try:
        job = process.stderr.readline().strip().split()[-1]
        claim = queue.claim(job, 20)
        queue.db.execute("BEGIN IMMEDIATE")
        evidence = drain_popen_process(process, process.pid)
        assert evidence.leader_stopped and evidence.process_group_stopped
        queue.db.rollback()
        with pytest.raises(ValueError, match="cancelled"):
            queue.complete(job, claim["token"], receipt())
        assert queue.inspect(job)[0]["state"] == "cancelled"
    finally:
        queue.db.rollback()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        queue.close()


def test_cancellation_during_completion_stays_visible(queue):
    job = queue.submit("review", None, None)
    claim = queue.claim(job, 20)

    def cancellation_before_update(statement):
        if statement.startswith("UPDATE jobs SET state='completed'"):
            queue.mark_cancelled(job)

    queue.db.set_trace_callback(cancellation_before_update)
    with pytest.raises(ValueError, match="cancelled"):
        queue.complete(job, claim["token"], receipt())
    queue.db.set_trace_callback(None)
    assert queue.inspect(job)[0]["state"] == "cancelled"
    queue.reconcile()
    assert queue.inspect(job)[0]["state"] == "cancelled"


def test_cancel_completed_is_noop_but_late_signal_fences(queue):
    job = queue.submit("review", None, None)
    claim = queue.claim(job, 20)
    queue.complete(job, claim["token"], receipt())
    queue.cancel(job)
    assert queue.inspect(job)[0]["state"] == "completed"
    queue.mark_cancelled(job)  # Signal after commit but before stdout publication.
    assert queue.inspect(job)[0]["state"] == "cancelled"
    with pytest.raises(ValueError, match="cancelled"):
        queue.complete(job, claim["token"], receipt())


@pytest.mark.parametrize("flag", ["--mod", "--mod=wrong", "--mo"])
def test_abbreviated_model_options_never_dispatch(tmp_path, flag):
    args = [
        sys.executable,
        "-m",
        "crewplane_dots_host",
        "--queue",
        str(tmp_path),
        "invoke",
        "--model",
        "gpt-6.1-sol",
        flag,
    ]
    if "=" not in flag:
        args.append("wrong")
    result = subprocess.run(
        args, input="review", capture_output=True, text=True, timeout=5
    )
    assert result.returncode != 0
    queue = Queue(tmp_path)
    try:
        assert queue.inspect() == []
    finally:
        queue.close()


@pytest.fixture
def invoke_options(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("review"))
    handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield argparse.Namespace(model=None, reasoning=None, timeout=20.0)
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)


@pytest.mark.race
def test_invocation_retries_submission_without_resetting_deadline(
    queue, invoke_options, monkeypatch, capsys
):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "time", lambda: 1000.0 + clock[0])
    with closing(Queue(queue.root)) as host:
        host.db.execute("BEGIN IMMEDIATE")

        def after_wait(seconds):
            clock[0] += seconds
            jobs = host.inspect()
            if not jobs:
                host.db.rollback()
                clock[0] += 2.0
                return
            assert len(jobs) == 1
            claim = host.claim(jobs[0]["id"], 30)
            host.complete(claim["id"], claim["token"], receipt())

        monkeypatch.setattr(time, "sleep", after_wait)
        assert invoke(queue, invoke_options) == 0

    jobs = queue.inspect()
    assert len(jobs) == 1
    assert jobs[0]["state"] == "completed"
    assert jobs[0]["request"]["prompt"] == "review"
    assert jobs[0]["request"]["expires_at"] == pytest.approx(
        1000.0 + invoke_options.timeout
    )
    captured = capsys.readouterr()
    assert captured.out == "actual result\n"
    assert captured.err == f"dots-host job {jobs[0]['id']}\n"


@pytest.mark.race
@pytest.mark.parametrize("outcome", ["terminate", "timeout"])
def test_submission_contention_preserves_cancellation_and_timeout(
    queue, invoke_options, monkeypatch, capsys, outcome
):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def after_busy_wait(seconds):
        clock[0] += seconds
        if outcome == "terminate":
            signal.raise_signal(signal.SIGTERM)
        else:
            clock[0] = invoke_options.timeout

    monkeypatch.setattr(time, "sleep", after_busy_wait)
    with closing(Queue(queue.root)) as host:
        host.db.execute("BEGIN IMMEDIATE")
        if outcome == "terminate":
            with pytest.raises(SystemExit) as error:
                invoke(queue, invoke_options)
            assert error.value.code == 128 + signal.SIGTERM
        else:
            with pytest.raises(ValueError, match="timed out"):
                invoke(queue, invoke_options)

    assert queue.inspect() == []
    assert len(list(queue.root.glob("*.cancelled"))) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.race
@pytest.mark.parametrize("outcome", ["complete", "cancel", "timeout"])
def test_invocation_survives_database_contention(
    queue, invoke_options, monkeypatch, capsys, outcome
):
    host = Queue(queue.root)
    submit = queue.submit
    clock = [0.0]

    def submit_with_contention(*args):
        job = submit(*args)
        host.claim(job, 30)
        host.db.execute("BEGIN IMMEDIATE")
        return job

    def after_busy_wait(seconds):
        clock[0] += seconds
        job = host.inspect()[0]
        if outcome == "complete":
            host.db.rollback()
            host.complete(job["id"], job["token"], receipt())
        elif outcome == "cancel":
            host.mark_cancelled(job["id"])
        else:
            clock[0] = invoke_options.timeout

    monkeypatch.setattr(queue, "submit", submit_with_contention)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", after_busy_wait)
    try:
        if outcome == "complete":
            assert invoke(queue, invoke_options) == 0
            assert capsys.readouterr().out == "actual result\n"
            assert queue.inspect()[0]["state"] == "completed"
        else:
            message = "cancelled" if outcome == "cancel" else "timed out"
            with pytest.raises(ValueError, match=message):
                invoke(queue, invoke_options)
            host.db.rollback()
            job = queue.inspect()[0]
            assert job["state"] == "cancelled"
            assert capsys.readouterr().out == ""
            with pytest.raises(ValueError, match="cancelled"):
                host.complete(job["id"], job["token"], receipt())
    finally:
        host.db.rollback()
        host.close()


@pytest.mark.parametrize("phase", ["submit", "poll"])
def test_invocation_propagates_other_database_errors(
    queue, invoke_options, monkeypatch, phase
):
    submit = queue.submit

    def submit_then_make_read_only(*args):
        job = submit(*args)
        queue.db.execute("PRAGMA query_only=ON")
        return job

    def unexpected_retry(seconds):
        pytest.fail(f"Unexpected {seconds}s retry for a non-contention database error")

    if phase == "submit":
        queue.db.execute("PRAGMA query_only=ON")
    else:
        monkeypatch.setattr(queue, "submit", submit_then_make_read_only)
    monkeypatch.setattr(time, "sleep", unexpected_retry)
    with pytest.raises(sqlite3.OperationalError) as error:
        invoke(queue, invoke_options)
    assert error.value.sqlite_errorcode == sqlite3.SQLITE_READONLY
    if phase == "submit":
        assert queue.inspect() == []
    else:
        assert queue.inspect()[0]["state"] == "cancelled"


@pytest.mark.race
def test_cli_startup_contention_keeps_invocation_deadline(
    queue: Queue,
    invoke_options: argparse.Namespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "time", lambda: 1000.0 + clock[0])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "crewplane-dots-host",
            "--queue",
            str(queue.root),
            "invoke",
            "--timeout",
            str(invoke_options.timeout),
        ],
    )
    queue.db.execute("BEGIN EXCLUSIVE")

    def service_after_wait(seconds: float) -> None:
        clock[0] += seconds
        jobs = queue.inspect()
        if not jobs:
            queue.db.rollback()
            clock[0] += 2.0
            return
        assert len(jobs) == 1
        claim = queue.claim(jobs[0]["id"], 30)
        queue.complete(claim["id"], claim["token"], receipt())

    monkeypatch.setattr(time, "sleep", service_after_wait)
    try:
        assert main() == 0
    finally:
        queue.db.rollback()

    jobs = queue.inspect()
    assert len(jobs) == 1
    assert jobs[0]["state"] == "completed"
    assert jobs[0]["request"]["prompt"] == "review"
    assert jobs[0]["request"]["expires_at"] == pytest.approx(
        1000.0 + invoke_options.timeout
    )
    captured = capsys.readouterr()
    assert captured.out == "actual result\n"
    assert captured.err == f"dots-host job {jobs[0]['id']}\n"


@pytest.mark.race
@pytest.mark.parametrize(
    ("phase", "completed"),
    [
        ("startup", False),
        ("read", False),
        ("write", False),
        ("startup", True),
        ("read", True),
    ],
)
def test_cli_cancel_retries_contention(
    queue: Queue,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    phase: str,
    completed: bool,
) -> None:
    job = queue.submit("review", None, None)
    token = queue.claim(job, 30)["token"]
    if completed:
        queue.complete(job, token, receipt())
    monkeypatch.setattr(
        sys, "argv", ["crewplane-dots-host", "--queue", str(queue.root), "cancel", job]
    )
    cancel = Queue.cancel
    locked = False

    def lock_before_cancel(contender: Queue, job_id: str) -> None:
        nonlocal locked
        if not locked:
            queue.db.execute("BEGIN EXCLUSIVE")
            locked = True
        cancel(contender, job_id)

    if phase == "read":
        monkeypatch.setattr(Queue, "cancel", lock_before_cancel)
    else:
        queue.db.execute("BEGIN IMMEDIATE" if phase == "write" else "BEGIN EXCLUSIVE")

    def release_after_contention(seconds: float) -> None:
        del seconds
        queue.db.rollback()

    monkeypatch.setattr(time, "sleep", release_after_contention)
    try:
        assert main() == 0
    finally:
        queue.db.rollback()

    if completed:
        assert queue.inspect(job)[0]["state"] == "completed"
        queue.complete(job, token, receipt())
    else:
        assert queue.inspect(job)[0]["state"] == "cancelled"
        with pytest.raises(ValueError, match="cancelled"):
            queue.complete(job, token, receipt())
    assert capsys.readouterr().err == ""


@pytest.mark.race
@pytest.mark.parametrize("command", ["invoke", "cancel"])
def test_cli_contention_exhausts_one_timeout(
    queue: Queue,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    job = queue.submit("review", None, None)
    arguments = ["--timeout", "2"] if command == "invoke" else [job]
    monkeypatch.setattr(
        sys,
        "argv",
        ["crewplane-dots-host", "--queue", str(queue.root), command, *arguments],
    )

    def exhaust_timeout(seconds: float) -> None:
        assert seconds <= 0.1
        assert clock[0] == 0.0, "Database retry restarted an exhausted timeout"
        clock[0] = 10.0

    monkeypatch.setattr(time, "sleep", exhaust_timeout)
    queue.db.execute("BEGIN EXCLUSIVE")
    try:
        assert main() == 1
    finally:
        queue.db.rollback()

    assert len(queue.inspect()) == 1
    assert queue.inspect(job)[0]["state"] == "pending"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "database is locked"


def test_failed_queue_initialization_closes_connection(
    queue: Queue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with closing(
        sqlite3.connect(queue.root / "queue.sqlite3", timeout=0.05)
    ) as connection:
        monkeypatch.setattr(sqlite3, "connect", Mock(return_value=connection))
        queue.db.execute("BEGIN EXCLUSIVE")
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                Queue(queue.root)
        finally:
            queue.db.rollback()
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_cli_startup_propagates_non_contention_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "queue.sqlite3").write_text("not a SQLite database", encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["crewplane-dots-host", "--queue", str(tmp_path), "invoke"]
    )

    def unexpected_retry(seconds: float) -> None:
        pytest.fail(f"Unexpected retry for {seconds}s")

    monkeypatch.setattr(time, "sleep", unexpected_retry)
    assert main() == 1
    assert capsys.readouterr().err.strip() == "file is not a database"


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf"])
def test_cli_rejects_invalid_timeout_before_opening_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    timeout: str,
) -> None:
    root = tmp_path / "queue"
    monkeypatch.setattr(
        sys,
        "argv",
        ["crewplane-dots-host", "--queue", str(root), "invoke", f"--timeout={timeout}"],
    )
    assert main() == 1
    assert not root.exists()
    assert capsys.readouterr().err.strip() == "Duration must be finite and positive"

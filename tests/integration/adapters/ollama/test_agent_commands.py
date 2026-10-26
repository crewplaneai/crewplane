import asyncio
import importlib
import json
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture
def executor(ollama_package):
    del ollama_package
    return importlib.import_module("crewplane_ollama.command_executor")


def execute(executor, arguments):
    async def run():
        return await asyncio.wait_for(executor.execute_command(arguments), 10)

    return asyncio.run(run())


@pytest.mark.parametrize("child_encoding", ["utf-8", "cp1252"])
def test_command_inherits_workspace_environment_and_literal_arguments(
    executor, tmp_path, monkeypatch, child_encoding
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_INDEX_FILE", "/controlled/index")
    monkeypatch.setenv("PYTHONIOENCODING", child_encoding)
    script = (
        "import json, os, sys; "
        "stdin = sys.stdin.buffer.read().decode('utf-8'); "
        "print(json.dumps([os.getcwd(), os.environ['GIT_INDEX_FILE'], sys.argv[1:], stdin]))"
    )
    result = execute(
        executor,
        {
            "argv": [sys.executable, "-c", script, "a b", "$HOME;echo wrong"],
            "stdin": "héllo",
        },
    )
    assert result["exit_code"] == 0
    assert json.loads(result["stdout"]) == [
        str(tmp_path),
        "/controlled/index",
        ["a b", "$HOME;echo wrong"],
        "héllo",
    ]
    assert result["stderr"] == ""
    assert result["stdout_bytes"] == len(result["stdout"].encode())
    assert result["stderr_bytes"] == 0
    assert result["stdout_truncated"] is False
    assert result["stderr_truncated"] is False


def test_missing_stdin_closes_pipe(executor):
    result = execute(
        executor,
        {"argv": [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"]},
    )
    assert result["stdout"] == f"''{os.linesep}"
    assert result["exit_code"] == 0


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_command_preserves_utf8_and_line_endings(executor, line_ending):
    content = f"héllo{line_ending}"
    script = (
        "import sys; data = sys.stdin.buffer.read(); "
        "sys.stdout.buffer.write(data); sys.stderr.buffer.write(data)"
    )
    result = execute(
        executor,
        {"argv": [sys.executable, "-c", script], "stdin": content},
    )
    assert result == {
        "exit_code": 0,
        "stdout": content,
        "stderr": content,
        "stdout_bytes": len(content.encode("utf-8")),
        "stderr_bytes": len(content.encode("utf-8")),
        "stdout_truncated": False,
        "stderr_truncated": False,
    }


def test_nonzero_exit_and_non_utf8_are_explicit_feedback(executor):
    result = execute(
        executor,
        {
            "argv": [
                sys.executable,
                "-c",
                "import os; os.write(1, b'\\xffa'); os.write(2, b'b\\xfe'); raise SystemExit(7)",
            ]
        },
    )
    assert result == {
        "exit_code": 7,
        "stdout": "\ufffda",
        "stderr": "b\ufffd",
        "stdout_bytes": 2,
        "stderr_bytes": 2,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }


def test_launch_failure_is_tool_feedback(executor, tmp_path):
    result = execute(executor, {"argv": [str(tmp_path / "missing executable")]})
    assert result["exit_code"] is None
    assert "FileNotFoundError" in result["error"]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"argv": []},
        {"argv": "echo bad"},
        {"argv": [""]},
        {"argv": ["  "]},
        {"argv": [42]},
        {"argv": ["echo", None]},
        {"argv": ["echo\0bad"]},
        {"argv": ["echo", "bad\0value"]},
        {"argv": ["echo"], "stdin": 1},
        {"argv": ["echo"], "stdin": "bad\0input"},
        {"argv": ["echo"], "stdin": "\ud800"},
        {"argv": ["echo"], "cwd": "/tmp"},
        {"argv": ["echo"], "env": {}},
    ],
)
def test_invalid_schema_never_spawns(executor, monkeypatch, arguments):
    async def unexpected_spawn(*args, **kwargs):
        del args, kwargs
        pytest.fail("invalid arguments must not spawn a command")

    async def run():
        monkeypatch.setattr(
            asyncio.get_running_loop(), "subprocess_exec", unexpected_spawn
        )
        return await executor.execute_command(arguments)

    result = asyncio.run(run())
    assert result["exit_code"] is None
    assert result["error"]


@pytest.mark.parametrize("name", ["script.cmd", "script.BAT", "plain-name"])
def test_windows_batch_file_rejected_including_path_resolution(
    executor, tmp_path, monkeypatch, name
):
    batch = tmp_path / "actual.cmd"
    batch.write_text("@echo unsupported")
    monkeypatch.setattr(executor.platform, "system", lambda: "Windows")

    def resolved_batch(value):
        assert value == name
        return str(batch)

    monkeypatch.setattr(executor.shutil, "which", resolved_batch)
    result = execute(executor, {"argv": [name]})
    assert result["exit_code"] is None
    assert "batch" in result["error"].lower()


def test_concurrent_large_io_is_drained_with_bounded_head_and_tail(executor):
    fixture = (
        Path(__file__).resolve().parents[3]
        / "fixtures/providers/ollama_command_flood.py"
    )
    count = 2 * 1024 * 1024
    result = execute(
        executor,
        {"argv": [sys.executable, str(fixture), str(count)], "stdin": "i" * count},
    )
    assert result["exit_code"] == 0
    assert len(result["stdout"].encode()) == 65536
    assert len(result["stderr"].encode()) == 65536
    assert result["stdout"].startswith("head-out:")
    assert result["stdout"].endswith(f":tail-out:{count}")
    assert result["stderr"].startswith("head-err:")
    assert result["stderr"].endswith(":tail-err")
    assert result["stdout_bytes"] == count + len(f"head-out::tail-out:{count}")
    assert result["stderr_bytes"] == count + len("head-err::tail-err")
    assert result["stdout_truncated"] is True
    assert result["stderr_truncated"] is True


@pytest.mark.parametrize("size", [0, 1, 32768, 32769, 65536, 65537])
def test_capture_boundaries_keep_exact_bytes(executor, size):
    result = execute(
        executor,
        {"argv": [sys.executable, "-c", f"import os; os.write(1, b'x' * {size})"]},
    )
    assert result["stdout"] == "x" * min(size, 65536)
    assert result["stdout_bytes"] == size
    assert result["stdout_truncated"] is (size > 65536)


@pytest.mark.skipif(os.name != "posix", reason="POSIX session identity")
def test_command_stays_in_outer_session_and_process_group(executor):
    result = execute(
        executor,
        {
            "argv": [
                sys.executable,
                "-c",
                "import os, json; print(json.dumps([os.getsid(0), os.getpgrp()]))",
            ]
        },
    )
    assert json.loads(result["stdout"]) == [os.getsid(0), os.getpgrp()]


def test_capture_never_retains_more_than_the_limit(executor):
    capture = executor.StreamCapture()
    capture.append(b"a" * 32768)
    for index in range(128):
        capture.append(b"b" * 65537)
        assert len(capture.head) + len(capture.tail) == 65536
        assert capture.total_bytes == 32768 + (index + 1) * 65537
    capture.append(b"c" * 32768)
    assert capture.text() == "a" * 32768 + "c" * 32768
    assert capture.truncated


def test_early_stdin_close_does_not_fail_completed_command(executor):
    result = execute(
        executor,
        {
            "argv": [sys.executable, "-c", "import os; os.close(0); print('finished')"],
            "stdin": "x" * (2 * 1024 * 1024),
        },
    )
    assert result["exit_code"] == 0
    assert result["stdout"] == f"finished{os.linesep}"


def test_windows_explicit_interpreter_does_not_reject_batch_argument(
    executor, monkeypatch
):
    monkeypatch.setattr(executor.platform, "system", lambda: "Windows")
    result = execute(
        executor,
        {
            "argv": [
                sys.executable,
                "-c",
                "import sys; print(sys.argv[1])",
                "script.cmd",
            ]
        },
    )
    assert result["exit_code"] == 0
    assert result["stdout"] == f"script.cmd{os.linesep}"


def test_stream_io_error_is_fatal_not_successful_tool_feedback(executor):
    async def run():
        protocol = executor.CommandProtocol()
        protocol.process_exited()
        protocol.pipe_connection_lost(0, None)
        protocol.pipe_connection_lost(1, OSError("capture failure"))
        protocol.pipe_connection_lost(2, None)
        with pytest.raises(ValueError, match="Command I/O failed.*capture failure"):
            await executor.collect_command(None, protocol)

    asyncio.run(run())

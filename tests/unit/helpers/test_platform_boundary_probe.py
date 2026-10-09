from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from crewplane.architecture.contracts import CommandResult
from tests.helpers import platform_boundary_probe as probe


def command_result(root: Path, name: str, returncode: int) -> CommandResult:
    stdout = root / f"{name}.stdout"
    stderr = root / f"{name}.stderr"
    stdout.write_bytes(b"exact\r\n\x1a\xff" if returncode == 0 else b"")
    stderr.write_bytes(
        b"diagnostic\n" if returncode == 0 else b"Provider launch failed\r\n"
    )
    return CommandResult(returncode, "", "", stdout, stderr)


@pytest.mark.parametrize("failure_kind", ["exception", "result"])
def test_capture_process_records_missing_command_and_cleans_captures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_kind: str
) -> None:
    success = command_result(tmp_path, "success", 0)
    launch_error = RuntimeError("Unable to start command")
    launch_error.__cause__ = FileNotFoundError("missing executable")
    runner = AsyncMock(
        side_effect=[
            success,
            launch_error
            if failure_kind == "exception"
            else command_result(tmp_path, "missing", 127),
        ]
    )
    monkeypatch.setattr(probe, "run_command_once", runner)
    (tmp_path / "provider.log").write_bytes(b"header\nexact\r\n\x1a\xffdiagnostic\n")

    captured = asyncio.run(probe.capture_process(tmp_path))

    assert captured["streams"] == {
        "stdout": b"exact\r\n\x1a\xff".hex(),
        "stderr": b"diagnostic\n".hex(),
    }
    assert captured["receipts"] == []
    assert captured["log"] == b"header\nexact\r\n\x1a\xffdiagnostic\n".hex()
    if failure_kind == "exception":
        assert captured["failure"] == {
            "type": "RuntimeError",
            "message": "Unable to start command",
            "cause": "FileNotFoundError",
            "cause_message": "missing executable",
        }
    else:
        assert captured["failure"] == {
            "returncode": 127,
            "stdout": "",
            "stderr": b"Provider launch failed\r\n".hex(),
        }
    assert not list(tmp_path.glob("*.stdout"))
    assert not list(tmp_path.glob("*.stderr"))


def test_capture_process_cleans_unexpected_missing_command_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        probe,
        "run_command_once",
        AsyncMock(
            side_effect=[
                command_result(tmp_path, "success", 0),
                command_result(tmp_path, "missing", 0),
            ]
        ),
    )
    with pytest.raises(AssertionError):
        asyncio.run(probe.capture_process(tmp_path))
    assert not list(tmp_path.glob("*.stdout"))
    assert not list(tmp_path.glob("*.stderr"))


def write_run(project: Path, output: bytes, summary: bytes) -> Path:
    stage = project / ".crewplane/execution-stages/run"
    results = project / ".crewplane/execution-results/run"
    results.mkdir(parents=True, exist_ok=True)
    (results / "result.md").write_bytes(output)
    (results / "report.bin").write_bytes(probe.GENERATED_BYTES)
    for name in ("manifests/nodes", "manifests/review-checkpoints", "logs"):
        (stage / name).mkdir(parents=True, exist_ok=True)
    (stage / "manifests/run.json").write_bytes(
        json.dumps(
            {"status": "succeeded", "run_id": "run", "workflow_signature": "signature"}
        ).encode()
    )
    (stage / "manifests/nodes/node.json").write_bytes(
        json.dumps(
            {
                "status": "succeeded",
                "node_id": "node",
                "artifacts": [
                    {
                        "relative_path": "result.md",
                        "size_bytes": len(output),
                        "sha256": hashlib.sha256(output).hexdigest(),
                    }
                ],
            }
        ).encode()
    )
    (stage / "manifests/review-checkpoints/node.json").write_bytes(b"{}")
    (stage / "logs/events.ndjson").write_bytes(b"")
    (stage / "logs/summary.md").write_bytes(summary)
    return stage


@pytest.mark.parametrize("field", ["results", "summary"])
def test_capture_run_detects_line_ending_changes_with_valid_manifest_hashes(
    tmp_path: Path, field: str
) -> None:
    text = "café\ncontrol-Z\x1a\n".encode()
    summary = b"Parallel:\n- `fanout` / z\n- `fanout` / a\n"
    stage = write_run(tmp_path, text, summary)
    original = probe.capture_run(tmp_path, stage)
    stage = write_run(
        tmp_path,
        text.replace(b"\n", b"\r\n") if field == "results" else text,
        summary.replace(b"\n", b"\r\n") if field == "summary" else summary,
    )

    changed = probe.capture_run(tmp_path, stage)

    assert original[field] != changed[field]
    if field == "results":
        assert changed[field] == {"result.md": text.decode().replace("\n", "\r\n")}
    else:
        assert changed[field] == "Parallel:\r\n- `fanout` / a\r\n- `fanout` / z\r\n"


def test_summary_normalization_preserves_bytes_around_parallel_rows(
    tmp_path: Path,
) -> None:
    text = "Parallel:\r\n- `fanout` / z\r\n- `fanout` / a\r\n\r\nOther\r\n"
    assert probe.normalized_summary(text, tmp_path, "run") == (
        "Parallel:\r\n- `fanout` / a\r\n- `fanout` / z\r\n\r\nOther\r\n"
    )

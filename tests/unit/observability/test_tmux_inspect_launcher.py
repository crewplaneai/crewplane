import json
from pathlib import Path

import pytest

from crewplane.observability.tmux import inspect_launcher
from crewplane.observability.tmux.snapshot_types import require_snapshot_string


def test_main_reports_missing_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing_snapshot(path: Path) -> None:
        del path
        return None

    monkeypatch.setattr(
        inspect_launcher,
        "read_inspect_snapshot",
        missing_snapshot,
    )

    assert inspect_launcher.main(["--snapshot", str(tmp_path / "missing")]) == 1
    assert capsys.readouterr().out == "Inspect snapshot unavailable.\n"


def test_main_rejects_selected_snapshot_as_inspect_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot_path = tmp_path / "snapshot"
    snapshot_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workflow_name": "workflow",
                "run_id": "run",
                "dashboard_generation": 1,
                "selection_generation": 0,
                "requested_selected_index": -1,
                "resolved_selected_index": 0,
                "node_count": 1,
                "node_id": "node.a",
                "written_at": 0.0,
                "log_file": "provider.log",
            }
        ),
        encoding="utf-8",
    )

    def reject_launch(snapshot: object) -> None:
        del snapshot
        pytest.fail("malformed inspect snapshot was launched")

    monkeypatch.setattr(inspect_launcher, "exec_raw", reject_launch)

    assert inspect_launcher.main(["--snapshot", str(snapshot_path)]) == 1
    assert capsys.readouterr().out == "Inspect snapshot unavailable.\n"


def test_main_launches_raw_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = {"log_file": "provider.log", "inspect_view": "raw"}
    launched: list[dict[str, object]] = []

    def read_snapshot(path: Path) -> dict[str, object]:
        del path
        return snapshot

    monkeypatch.setattr(inspect_launcher, "read_inspect_snapshot", read_snapshot)
    monkeypatch.setattr(inspect_launcher, "exec_raw", launched.append)

    assert inspect_launcher.main(["--snapshot", str(tmp_path / "snapshot")]) == 0
    assert launched == [snapshot]


def test_main_reports_launch_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def read_snapshot(path: Path) -> dict[str, object]:
        del path
        return {"log_file": "provider.log", "inspect_view": "raw"}

    monkeypatch.setattr(inspect_launcher, "read_inspect_snapshot", read_snapshot)

    def fail_launch(_snapshot: dict[str, object]) -> None:
        del _snapshot
        raise OSError("tail unavailable")

    monkeypatch.setattr(inspect_launcher, "exec_raw", fail_launch)

    assert inspect_launcher.main(["--snapshot", str(tmp_path / "snapshot")]) == 1
    assert capsys.readouterr().out == "Inspect launch failed: OSError\n"


def test_exec_raw_uses_tail_follow_after_option_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        inspect_launcher.os,
        "execvp",
        lambda executable, command: calls.append((executable, command)),
    )

    inspect_launcher.exec_raw({"log_file": "-provider.log"})

    assert calls == [("tail", ["tail", "-n", "+1", "-F", "--", "-provider.log"])]


def test_exec_formatted_validates_descriptor_and_relaunches_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        inspect_launcher.os,
        "execv",
        lambda executable, command: calls.append((executable, command)),
    )

    inspect_launcher.exec_formatted(
        {
            "log_presentation_format": "json_lines",
            "log_presentation_profile": "codex",
        },
        snapshot_path,
    )

    assert calls == [
        (
            inspect_launcher.sys.executable,
            [
                inspect_launcher.sys.executable,
                "-m",
                "crewplane.observability.log_presentation.follow",
                "--snapshot",
                str(snapshot_path),
            ],
        )
    ]


def test_require_string_rejects_missing_value() -> None:
    assert require_snapshot_string({"value": "ok"}, "value") == "ok"
    with pytest.raises(ValueError, match="snapshot missing value"):
        require_snapshot_string({"value": None}, "value")

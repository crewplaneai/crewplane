from pathlib import Path

import pytest

from crewplane.observability.tmux import inspect_launcher


def test_main_reports_missing_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing_snapshot(path: Path) -> None:
        del path
        return None

    monkeypatch.setattr(inspect_launcher, "read_snapshot", missing_snapshot)

    assert inspect_launcher.main(["--snapshot", str(tmp_path / "missing")]) == 1
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

    monkeypatch.setattr(inspect_launcher, "read_snapshot", read_snapshot)
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
        return {"log_file": "provider.log"}

    monkeypatch.setattr(inspect_launcher, "read_snapshot", read_snapshot)

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
    assert inspect_launcher.require_string({"value": "ok"}, "value") == "ok"
    with pytest.raises(ValueError, match="snapshot missing value"):
        inspect_launcher.require_string({"value": None}, "value")

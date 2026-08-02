import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from crewplane.observability.log_presentation import follow


def test_main_renders_one_snapshot_when_once_is_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    rendered: list[Path] = []
    monkeypatch.setattr(follow, "render_snapshot", rendered.append)

    assert follow.main(["--snapshot", str(snapshot_path), "--once"]) == 0
    assert rendered == [snapshot_path]


def test_render_snapshot_formats_notices_and_log_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = tmp_path / "provider.log"
    log_path.write_text("provider output", encoding="utf-8")
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "log_file": str(log_path),
                "log_presentation_format": "plain",
                "log_presentation_profile": "generic",
                "invocation_status": "succeeded",
                "line_budget": 12,
            }
        ),
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    def fake_format_log_file(**kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(
            notices=[SimpleNamespace(message="output was truncated")],
            lines=["provider output"],
        )

    monkeypatch.setattr(follow, "format_log_file", fake_format_log_file)
    monkeypatch.setattr(follow, "time", lambda: 123.0)

    follow.render_snapshot(snapshot_path)

    assert capsys.readouterr().out == (
        "\033[H\033[Joutput was truncated\nprovider output\n"
    )
    assert calls == [
        {
            "log_path": log_path,
            "descriptor": follow.LogPresentationDescriptor(
                format="plain", profile="generic"
            ),
            "line_budget": 12,
            "invocation_status": "succeeded",
            "wall_time_now": 123.0,
            "limits": follow.DEFAULT_LIMITS,
        }
    ]


def test_render_snapshot_uses_defaults_for_empty_optional_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        """{
          "log_file": "provider.log",
          "log_presentation_format": "plain",
          "log_presentation_profile": "generic",
          "invocation_status": "unknown",
          "line_budget": 0
        }""",
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    def fake_format_log_file(**kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(notices=[], lines=[])

    monkeypatch.setattr(follow, "format_log_file", fake_format_log_file)

    follow.render_snapshot(snapshot_path)

    assert capsys.readouterr().out == ("\033[H\033[JNo formatted log output yet.\n")
    assert calls[0]["line_budget"] == follow.DEFAULT_FORMATTED_INSPECT_LINE_BUDGET
    assert calls[0]["invocation_status"] == "running"


@pytest.mark.parametrize(
    ("contents", "error_name"),
    [
        pytest.param("[]", "ValueError", id="non-object"),
        pytest.param("{bad", "JSONDecodeError", id="invalid-json"),
        pytest.param(
            '{"log_presentation_format":"plain"}',
            "ValueError",
            id="missing-required-field",
        ),
    ],
)
def test_render_snapshot_reports_bounded_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    contents: str,
    error_name: str,
) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(contents, encoding="utf-8")

    follow.render_snapshot(snapshot_path)

    assert capsys.readouterr().out == (
        f"\033[H\033[JFormatted log unavailable: {error_name}\n"
    )


def test_snapshot_helpers_validate_values(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text('{"value":"present"}', encoding="utf-8")

    assert follow.read_snapshot(snapshot_path) == {"value": "present"}
    assert follow.status_from_snapshot({}) == "running"
    assert follow.status_from_snapshot({"invocation_status": "failed"}) == "failed"
    assert follow.require_string({"value": "present"}, "value") == "present"
    with pytest.raises(ValueError, match="snapshot missing value"):
        follow.require_string({"value": ""}, "value")

from copy import deepcopy

import pytest

from crewplane.adapters.invokers.cli_invoker.providers.opencode_events import (
    extract_opencode_output,
)
from crewplane.architecture.contracts import CommandResult
from tests.helpers.opencode import (
    event,
    finish_event,
    fixture_text,
    native_tokens,
    stream,
    text_event,
)


def extract(stdout: str, stderr: str = ""):
    return extract_opencode_output(CommandResult(0, stdout, stderr), None)


@pytest.mark.parametrize(
    "fixture,expected",
    [
        ("simple", "Answer λ\n"),
        ("multistep", "Final answer\nSecond part\n"),
    ],
)
def test_only_completed_final_message_text_is_selected(fixture, expected):
    result = extract(fixture_text(fixture))
    assert result.output_extraction_status == "success"
    assert result.output_text == expected
    assert result.output_char_count == len(expected)
    assert result.output_path is None
    assert not result.owns_output_path


def test_malformed_usage_does_not_discard_answer():
    result = extract(
        stream(text_event(), finish_event(tokens=native_tokens(input="invalid")))
    )
    assert result.output_extraction_status == "success"
    assert result.output_text == "Answer λ\n"


@pytest.mark.parametrize(
    "parts,expected",
    [
        (["  literal λ\n", "\nsecond  "], "  literal λ\n\n\nsecond  \n"),
        (["\n \t", "kept\n"], "kept\n"),
        (["one\n\n"], "one\n\n"),
    ],
)
def test_text_is_preserved_joined_and_terminated(parts, expected):
    result = extract(
        stream(
            *(text_event(text, str(index)) for index, text in enumerate(parts)),
            finish_event(),
        )
    )
    assert result.output_text == expected


def test_identical_snapshots_do_not_repeat_text_or_reactivate_old_message():
    first = text_event("old")
    first_finish = finish_event()
    last = text_event("new", "text-2", message="message-2")
    last_finish = finish_event(part_id="finish-2", message="message-2")
    result = extract(
        stream(first, first_finish, last, last, last_finish, first, first_finish)
    )
    assert result.output_text == "new\n"


@pytest.mark.parametrize(
    "update",
    [
        {"text": "changed"},
        {"messageID": "other"},
        {"time": {"end": 3}},
    ],
)
def test_conflicting_completed_text_snapshots_are_malformed(update):
    original = text_event()
    duplicate = deepcopy(original)
    duplicate["part"].update(update)
    assert (
        extract(stream(original, finish_event(), duplicate)).output_extraction_status
        == "malformed"
    )


def test_conflicting_finish_reason_and_part_type_are_malformed():
    for conflicting in (finish_event("length"), event("step_start", "text-1")):
        assert (
            extract(
                stream(text_event(), finish_event(), conflicting)
            ).output_extraction_status
            == "malformed"
        )


@pytest.mark.parametrize("kind", ["step_start", "text", "tool_use", "step_finish"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("sessionID", None),
        ("sessionID", " "),
        ("messageID", 1),
        ("messageID", ""),
        ("id", False),
        ("id", "\t"),
    ],
)
def test_consumed_parts_require_nonblank_string_identities(kind, field, value):
    malformed = event(kind, "bad", text="text", time={"end": 2}, reason="stop")
    malformed["part"][field] = value
    assert (
        extract(
            stream(text_event(), finish_event(), malformed)
        ).output_extraction_status
        == "malformed"
    )


@pytest.mark.parametrize(
    "change", ["envelope", "other_session", "missing_part", "nonobject_part"]
)
def test_consumed_events_require_consistent_session_and_part_shape(change):
    malformed = text_event(part_id="text-2")
    match change:
        case "envelope":
            malformed["sessionID"] = "different"
        case "other_session":
            malformed["sessionID"] = malformed["part"]["sessionID"] = "session-2"
        case "missing_part":
            del malformed["part"]
        case "nonobject_part":
            malformed["part"] = []
    assert (
        extract(
            stream(text_event(), finish_event(), malformed)
        ).output_extraction_status
        == "malformed"
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"text": None},
        {"text": 1},
        {"time": None},
        {"time": {}},
        {"time": {"end": None}},
        {"time": {"end": False}},
        {"time": {"end": "2"}},
        {"time": {"end": -1}},
        {"time": {"end": float("nan")}},
    ],
)
def test_text_requires_string_and_completed_timestamp(fields):
    malformed = text_event()
    malformed["part"].update(fields)
    assert (
        extract(stream(malformed, finish_event())).output_extraction_status
        == "malformed"
    )


@pytest.mark.parametrize("reason", [None, 1, {}, False])
def test_finish_reason_must_be_a_string(reason):
    assert (
        extract(stream(text_event(), finish_event(reason))).output_extraction_status
        == "malformed"
    )


@pytest.mark.parametrize(
    "stdout",
    ["not json", "[]", "null", "42", '{"type":', fixture_text("simple") + "not json"],
)
def test_invalid_json_or_nonobjects_are_malformed(stdout):
    assert extract(stdout).output_extraction_status == "malformed"


@pytest.mark.parametrize("reason", ["length", "tool-calls", "unknown", ""])
def test_only_stop_establishes_completion(reason):
    assert (
        extract(stream(text_event(), finish_event(reason))).output_extraction_status
        == "missing"
    )


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        " \n\t\n",
        stream(text_event()),
        stream(finish_event()),
        stream(text_event(" \n"), finish_event()),
        stream(
            text_event(), finish_event(), event("step_start", "start-2", "message-2")
        ),
        stream(
            text_event(),
            finish_event(),
            text_event("unfinished", "text-2", message="message-2"),
        ),
        stream(text_event(), finish_event(), event("tool_use", "tool-2", "message-2")),
        stream(text_event(), finish_event(), event("step_start", "start-2")),
        stream(
            text_event(), finish_event(part_id="other-finish", message="other-message")
        ),
        stream(text_event(), {"type": "unknown", "reason": "stop"}),
    ],
)
def test_incomplete_or_empty_final_message_never_falls_back(stdout):
    result = extract(stdout)
    assert result.output_extraction_status == "missing"
    assert result.output_text == ""


def test_unknown_events_reasoning_and_tool_payloads_are_not_answer_text():
    result = extract(
        stream(
            {"type": "unknown", "part": "anything"},
            {"type": "reasoning", "text": "private reasoning"},
            event(
                "tool_use", "tool-1", state={"status": "error", "error": "recoverable"}
            ),
            text_event(),
            finish_event(),
            {"type": "unknown", "sessionID": "unrelated"},
        )
    )
    assert result.output_extraction_status == "success"
    assert result.output_text == "Answer λ\n"


def test_top_level_error_after_text_prevents_publication():
    result = extract(
        fixture_text() + stream({"type": "error", "error": {"message": "failed"}})
    )
    assert result.output_extraction_status == "malformed"
    assert result.output_text == ""


@pytest.mark.parametrize(
    "contents,expected", [(fixture_text(), "success"), (" \n", "missing")]
)
def test_captured_stdout_is_authoritative_and_borrowed(tmp_path, contents, expected):
    capture = tmp_path / "stdout.jsonl"
    capture.write_text(
        contents + (stream({"type": "unknown"}) * 300 if contents.strip() else "")
    )
    before = capture.read_bytes()
    result = extract_opencode_output(
        CommandResult(0, "invalid inline tail", "diagnostics", stdout_path=capture),
        None,
    )
    assert result.output_extraction_status == expected
    assert capture.read_bytes() == before
    assert list(tmp_path.iterdir()) == [capture]


def test_stderr_is_never_an_answer():
    assert extract("", fixture_text()).output_extraction_status == "missing"
    assert extract(fixture_text(), "bad JSON\n").output_text == "Answer λ\n"

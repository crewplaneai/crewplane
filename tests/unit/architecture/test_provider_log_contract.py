from pathlib import Path

import pytest

from crewplane.architecture.contracts.provider_log import (
    build_provider_log_header,
    provider_log_body_start,
)


@pytest.mark.parametrize("model", [None, "", "mødel"])
@pytest.mark.parametrize("reasoning", [None, "", "high"])
def test_initial_provider_header_preserves_bytes_and_optional_fields(
    model: str | None, reasoning: str | None
) -> None:
    header = build_provider_log_header(
        "now", "/bin/cli", model, Path("résultat.md"), reasoning
    )
    model_label = "provider default" if model is None else model
    reasoning_line = "" if reasoning is None else f"requested_reasoning: {reasoning}\n"
    assert (
        header
        == (
            f"started_at: now\ncli_executable: /bin/cli\nmodel: {model_label}\n"
            f"{reasoning_line}output_file: résultat.md\n---\n"
        ).encode()
    )
    assert provider_log_body_start(header + b"body\n") == len(header)


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
@pytest.mark.parametrize("final_newline", [False, True])
def test_provider_header_recognition_preserves_whitespace_and_delimiter_tolerance(
    newline: bytes, final_newline: bool
) -> None:
    header = newline.join(
        [
            b"",
            b"  started_at:  ",
            b"cli_executable:",
            b"model:",
            b" requested_reasoning:  ",
            b"output_file:",
            b"---",
        ]
    )
    if final_newline:
        header += newline

    assert provider_log_body_start(header) == len(header)


@pytest.mark.parametrize(
    "head",
    [
        b"",
        b"---\n",
        b"started_at: now\ncli_executable: cli\nmodel: m\noutput_file: out\n--",
        b"started_at: now\ncli_executable: cli\noutput_file: out\n---\n",
        b"started_at: now\ncli_executable: cli\nunknown: m\noutput_file: out\n---\n",
        b"started_at: now\nmodel: m\ncli_executable: cli\noutput_file: out\n---\n",
        b"started_at: now\ncli_executable: cli\nmodel: m\noutput_file: out\nextra: value\n---\n",
        b"started_at: now\ncli_executable: cli\nmodel: m\noutput_file: out\nrequested_reasoning: high\n---\n",
    ],
)
def test_provider_header_recognition_rejects_unknown_or_incomplete_headers(
    head: bytes,
) -> None:
    assert provider_log_body_start(head) == 0

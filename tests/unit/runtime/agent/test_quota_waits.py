from datetime import UTC, datetime

import pytest

from crewplane.runtime.agent.quota.waits import extract_wait_candidates_from_line

NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param("Retry after 1.5s", [1.5], id="decimal-seconds"),
        pytest.param("Try again in 2 minutes 3 seconds", [123.0], id="compound"),
        pytest.param("Retrying after 250ms", [0.25], id="milliseconds"),
        pytest.param("Reset after 1 hour 2 mins", [3720.0], id="hours-minutes"),
        pytest.param(
            "Quota will reset after 1 day",
            [86400.0, 86400.0, 86400.0],
            id="overlapping-reset-phrases",
        ),
        pytest.param("Retry after eventually", [], id="missing-duration"),
    ],
)
def test_relative_waits(line: str, expected: list[float]) -> None:
    assert extract_wait_candidates_from_line(line, NOW) == expected


def test_multiple_relative_phrases_are_all_reported() -> None:
    waits = extract_wait_candidates_from_line(
        "Retry after 2s; please retry in 3s",
        NOW,
    )

    assert sorted(waits) == [3.0, 5.0]


@pytest.mark.parametrize(
    ("line", "expected_seconds"),
    [
        pytest.param(
            "resetAt=2026-04-10T12:01:30Z",
            90.0,
            id="iso-zulu",
        ),
        pytest.param(
            "reset_at=2026-04-10T14:01:30+0200",
            90.0,
            id="iso-compact-offset",
        ),
        pytest.param(
            "reset-at=1775822490",
            90.0,
            id="epoch-seconds",
        ),
        pytest.param(
            "x-codex-primary-reset-at=1775822490000",
            90.0,
            id="epoch-milliseconds",
        ),
        pytest.param(
            "quota reached |1775822490",
            90.0,
            id="pipe-epoch",
        ),
    ],
)
def test_absolute_machine_readable_waits(
    line: str,
    expected_seconds: float,
) -> None:
    assert extract_wait_candidates_from_line(line, NOW) == [expected_seconds]


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("resetAt=not-an-epoch", id="invalid-epoch"),
        pytest.param("resetAt=2026-99-99T12:00:00Z", id="invalid-iso"),
        pytest.param("resetAt=2026-04-10T11:00:00Z", id="past-iso"),
        pytest.param("reset-at=1775818800", id="past-epoch"),
        pytest.param("unrelated |1775822490", id="pipe-without-context"),
    ],
)
def test_invalid_or_past_machine_readable_waits_are_ignored(line: str) -> None:
    assert extract_wait_candidates_from_line(line, NOW) == []


def test_timezone_qualified_local_time_rolls_to_next_day() -> None:
    waits = extract_wait_candidates_from_line(
        "Your limit will reset at 7 am (America/New_York)",
        NOW,
    )

    assert waits == [23 * 60 * 60.0]


def test_invalid_timezone_qualified_local_time_is_ignored() -> None:
    assert (
        extract_wait_candidates_from_line(
            "Your limit will reset at 7 am (Invalid/Timezone)",
            NOW,
        )
        == []
    )


@pytest.mark.parametrize(
    ("line", "expected_seconds"),
    [
        pytest.param(
            "Quota resets at Apr 10, 2026 1:30 PM",
            5400.0,
            id="abbreviated-month",
        ),
        pytest.param(
            "Try again at April 10th 2026 2 PM",
            7200.0,
            id="ordinal-long-month",
        ),
    ],
)
def test_human_readable_absolute_waits(
    line: str,
    expected_seconds: float,
) -> None:
    host_offset = NOW.astimezone().utcoffset()
    assert host_offset is not None

    assert extract_wait_candidates_from_line(line, NOW) == [
        expected_seconds - host_offset.total_seconds()
    ]


def test_past_human_readable_absolute_wait_is_ignored() -> None:
    assert (
        extract_wait_candidates_from_line(
            "Quota resets at April 10th 2020 11 AM",
            NOW,
        )
        == []
    )

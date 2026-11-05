"""Bound relative-prefix query and scoring work in a test-owned subprocess."""

from __future__ import annotations

import sys

from crewplane.artifacts.memory.store import enforce_bounds
from crewplane.core.memory.candidates import LessonProposal
from crewplane.core.memory.models import MemorySnapshot
from crewplane.core.memory.query import MAX_QUERY_CHARS, QueryContribution, build_query
from crewplane.core.memory.retrieval import select_memory
from tests.helpers.memory import accepted_snapshot


def check_query(prefix: str) -> None:
    task = QueryContribution(kind="literal", source="task", text="Inspect")
    for count in (10000, 11000):
        content = prefix * count
        query = build_query(
            (task, QueryContribution(kind="file", source="context", text=content))
        )
        assert query.terms == ("inspect",) and not query.identifiers
        assert query.examined_chars == min(
            MAX_QUERY_CHARS, len(task.text) + len(content)
        )
        selection = select_memory(MemorySnapshot(), "work", "executor", query)
        assert not selection.selected and not selection.rendered
    assert query.contributions[-1].text.endswith(".")
    assert any("character limit" in item.reason for item in query.exclusions)


def check_accepted(prefix: str) -> None:
    count = 10000 if prefix == "../" else 7000
    snapshot = accepted_snapshot(
        *(
            LessonProposal(title="Unrelated", body=prefix * count + tail)
            for tail in ("", "..")
        )
    )
    enforce_bounds(snapshot)
    task = QueryContribution(kind="literal", source="task", text="Inspect")
    selection = select_memory(
        snapshot, "work", "executor", build_query((task,)), max_records=1
    )
    assert not selection.selected and not selection.rendered
    assert "No accepted records match" in selection.omissions[0]


if __name__ == "__main__":
    prefix, source = sys.argv[1:]
    if source == "query":
        check_query(prefix)
    else:
        check_accepted(prefix)

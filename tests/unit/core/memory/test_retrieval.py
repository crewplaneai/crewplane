from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from crewplane.core.memory.candidates import LessonProposal
from crewplane.core.memory.models import MemorySnapshot
from crewplane.core.memory.query import (
    MAX_QUERY_CHARS,
    MAX_QUERY_TERMS,
    QueryContribution,
    QueryExclusion,
    build_query,
    tokens,
)
from crewplane.core.memory.retrieval import (
    MemorySelection,
    score_revision,
    select_memory,
)
from tests.helpers.memory import accepted_snapshot


def query(text: str):
    return build_query((QueryContribution(kind="literal", source="task", text=text),))


def test_unicode_identifiers_paths_components_and_stop_words():
    normalized, identifiers = tokens("Ｔｈｅ src/FooBar.py snake_case Straße")
    assert "the" not in normalized
    assert {
        "src/foobar.py",
        "src",
        "foo",
        "bar",
        "py",
        "snake_case",
        "snake",
        "case",
        "strasse",
    } <= set(normalized)
    assert "src/foobar.py" in identifiers
    assert query("STRASSE Straße").terms == ("strasse",)


@pytest.mark.parametrize(
    "path",
    [
        ".gitignore",
        ".pre-commit-config.yaml",
        ".config/settings.json",
        "./.config/settings.json",
        "../.config/settings.json",
        "/.config/settings.json",
    ],
)
def test_leading_dot_paths_remain_whole_identifiers(path):
    terms, identifiers = tokens(f"Inspect {path}.")

    assert identifiers == (path,)
    assert path in terms
    assert all(
        component in terms
        for component in path.replace("/", ".").replace("-", ".").split(".")
        if component
    )


@pytest.mark.parametrize(
    "path,shorter_path",
    [
        ("../../src/file.py", "../src/file.py"),
        ("../../../src/file.py", "../../src/file.py"),
        ("./../../.config/settings.json", "../../.config/settings.json"),
        (r"..\..\src\file.py", r"..\src\file.py"),
    ],
)
def test_relative_path_prefixes_remain_complete_distinct_identifiers(
    path: str, shorter_path: str
) -> None:
    terms, identifiers = tokens(f"Inspect {path}. {shorter_path}.")

    assert identifiers == (path, shorter_path)
    assert path in terms
    assert shorter_path in terms
    assert "file" not in identifiers
    assert "settings" not in identifiers
    assert set(tokens(path)[1]).isdisjoint(tokens(shorter_path)[1])


@pytest.mark.parametrize("prefix", ["../", "./", "..\\", ".\\"])
@pytest.mark.parametrize("tail", ["", ".", ".."])
def test_prefix_only_paths_do_not_hide_following_complete_tokens(
    prefix: str, tail: str
) -> None:
    terms, identifiers = tokens(f"{prefix * 5}{tail} ../../.config/settings.json.")

    assert terms == ("../../.config/settings.json", "config", "settings", "json")
    assert identifiers == ("../../.config/settings.json",)


@pytest.mark.parametrize("prefix", ["../", "..\\"])
@pytest.mark.parametrize("source", ["query", "accepted"])
def test_prefix_only_and_truncated_relative_paths_complete_within_bounds(
    prefix: str, source: str
) -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.fixtures.memory.prefix_worker",
            prefix,
            source,
        ],
        cwd=Path(__file__).resolve().parents[4],
        check=True,
        timeout=5,
    )


def test_exact_tag_ranking_normalizes_unicode_and_case():
    task = build_query(
        (QueryContribution(kind="identifier", source="node", text="build"),)
    )
    scores = [
        score_revision(
            task,
            accepted_snapshot(
                LessonProposal(title="Lesson", body="Details", tags=(tag,))
            )
            .records[0]
            .revisions[0],
        )
        for tag in ("build", "BUILD", "ｂｕｉｌｄ")
    ]
    assert scores == [(1, 1, 0)] * 3


@pytest.mark.parametrize(
    "text,identifier,components",
    [
        ("src/FooBar.py", "src/foobar.py", ("src", "foo", "bar", "py")),
        ("read_file", "read_file", ("read", "file")),
        ("ReadFile", "readfile", ("read", "file")),
        ("ＢＵＩＬＤ", "build", ()),
    ],
)
def test_identifier_contributions_keep_only_whole_tokens_in_primary_tier(
    text, identifier, components
):
    task = build_query(
        (QueryContribution(kind="identifier", source="node", text=text),)
    )

    assert task.identifiers == (identifier,)
    assert task.terms == (identifier, *components)


def test_authored_priority_and_bounded_query_diagnostics():
    contributions = (
        QueryContribution(kind="file", source="large", text="unhelpful " * 5000),
        QueryContribution(kind="identifier", source="node", text="src/important.py"),
    )
    result = build_query(
        contributions,
        (QueryExclusion(source="env:SECRET", reason="environment excluded"),),
    )
    assert result.terms[:1] == ("src/important.py",)
    assert result.examined_chars == MAX_QUERY_CHARS
    assert any("character limit" in item.reason for item in result.exclusions)
    assert result.exclusions[0].source == "env:SECRET"
    many = query(" ".join(f"term{number}" for number in range(300)))
    assert len(many.terms) == MAX_QUERY_TERMS
    assert any("term limit" in item.reason for item in many.exclusions)


def test_accepted_only_deterministic_ranking_and_complete_packing():
    snapshot = accepted_snapshot(
        LessonProposal(title="Path", body="Use src/read_file.py safely."),
        LessonProposal(title="Read file", body="Apply file reads."),
        LessonProposal(title="No match", body="Unrelated ocean topic."),
    )
    selected = select_memory(snapshot, "build", "executor", query("src/read_file.py"))
    assert selected.selected[0].revision.title == "Path"
    assert len(selected.selected) == 2
    assert selected == select_memory(
        snapshot, "build", "executor", query("src/read_file.py")
    )
    exact = select_memory(
        snapshot, "build", "executor", query("src/read_file.py"), max_records=1
    )
    assert len(exact.selected) == 1
    assert any("record limit" in item for item in exact.omissions)
    assert "historical evidence" in selected.rendered
    assert "authoritative project guidance" in selected.rendered
    assert "2026" not in selected.rendered
    too_small = select_memory(
        snapshot, "build", "executor", query("src/read_file.py"), max_chars=2
    )
    assert too_small.rendered == ""
    assert any("No whole" in item for item in too_small.omissions)


def test_oversized_first_record_does_not_hide_later_fit():
    snapshot = accepted_snapshot(
        LessonProposal(title="src/file.py", body="x" * 9000),
        LessonProposal(title="File", body="Keep file checks."),
    )
    result = select_memory(snapshot, "build", "executor", query("src/file.py"))
    assert [item.revision.title for item in result.selected] == ["File"]
    assert "complete record" in result.omissions[0]


@pytest.mark.parametrize(
    "text,reason", [("the and of", "Insufficient"), ("banana", "No accepted")]
)
def test_no_context_and_no_match_are_explained(text, reason):
    result = select_memory(MemorySnapshot(), "node", "executor", query(text))
    assert not result.rendered
    assert reason in result.omissions[0]


@pytest.mark.parametrize(
    "tamper",
    [
        "rendered",
        "rendered_chars",
        "max_chars",
        "policy",
        "terms",
        "chars",
        "digest",
        "role",
    ],
)
def test_frozen_selection_rejects_tampering(tamper):
    selected = select_memory(
        accepted_snapshot(LessonProposal(title="Files", body="Protect files.")),
        "build",
        "executor",
        query("files"),
    )
    payload = selected.model_dump(mode="json")
    if tamper == "rendered":
        payload["rendered"] += "injected"
    elif tamper == "rendered_chars":
        payload["rendered_chars"] += 1
    elif tamper == "max_chars":
        payload["max_chars"] = 1
    elif tamper == "policy":
        payload["retrieval_revision"] = 999
    elif tamper == "terms":
        payload["query"]["terms"] = ["other"]
    elif tamper == "chars":
        payload["query"]["examined_chars"] = 0
    elif tamper == "digest":
        payload["selected"][0]["revision"]["content_digest"] = "0" * 64
    else:
        payload["role"] = "system"
    with pytest.raises(ValidationError):
        MemorySelection.model_validate(payload)

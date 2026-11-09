from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from crewplane.core.memory import query as memory_query
from crewplane.core.memory.candidates import LessonProposal
from crewplane.core.memory.models import MemorySnapshot
from crewplane.core.memory.query import (
    MAX_QUERY_CHARS,
    MAX_QUERY_TERMS,
    QUERY_REVISION,
    QueryContribution,
    QueryExclusion,
    build_query,
    tokens,
)
from crewplane.core.memory.rendering import render_block
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


@pytest.mark.parametrize("kind", ["file", "input"])
@pytest.mark.parametrize(
    "padding,reason",
    [
        ("padding " * 5000, "character limit"),
        (" ".join(f"preface{index}" for index in range(300)), "term limit"),
    ],
    ids=["character-pressure", "term-pressure"],
)
def test_query_budget_preserves_later_current_context(kind, padding, reason):
    contributions = (
        QueryContribution(kind="literal", source="instructions", text=padding),
        QueryContribution(kind=kind, source="current", text="src/CurrentReader.py"),
    )
    result = build_query(contributions)

    assert {"src/currentreader.py", "src", "current", "reader", "py"} <= set(
        result.terms
    )
    assert result.identifiers == ("src/currentreader.py",)
    assert result.contributions[-1] == contributions[-1]
    assert result.examined_chars <= MAX_QUERY_CHARS
    assert len(result.terms) <= MAX_QUERY_TERMS
    assert any(
        item.source == "instructions" and reason in item.reason
        for item in result.exclusions
    )
    assert not any(item.source == "current" for item in result.exclusions)
    reconstructed = build_query(result.contributions)
    assert reconstructed.terms == result.terms
    assert reconstructed.identifiers == result.identifiers
    assert reconstructed.contributions == result.contributions
    assert reconstructed.examined_chars == result.examined_chars
    assert build_query(reconstructed.contributions) == reconstructed
    selection = select_memory(MemorySnapshot(), "work", "executor", result)
    assert MemorySelection.model_validate_json(selection.model_dump_json()) == selection


def test_query_term_budget_shares_capacity_and_keeps_source_order():
    result = build_query(
        tuple(
            QueryContribution(
                kind=kind,
                source=prefix,
                text=" ".join(f"{prefix}{index}" for index in range(256)),
            )
            for kind, prefix in (("literal", "left"), ("file", "right"))
        )
    )

    assert result.terms == tuple(
        f"{prefix}{index}" for prefix in ("left", "right") for index in range(128)
    )
    assert {item.source for item in result.exclusions} == {"left", "right"}
    assert all("term limit" in item.reason for item in result.exclusions)


def test_query_character_budget_redistributes_unused_shares():
    result = build_query(
        (
            QueryContribution(kind="literal", source="long", text="x " * 32_000),
            QueryContribution(kind="identifier", source="short", text="node"),
            QueryContribution(kind="file", source="file", text="y " * 32_000),
            QueryContribution(kind="input", source="input", text="z " * 32_000),
        )
    )
    lengths = {item.source: len(item.text) for item in result.contributions}

    assert lengths["short"] == 4
    assert sum(lengths.values()) == result.examined_chars == MAX_QUERY_CHARS
    assert (
        max(lengths[key] for key in ("long", "file", "input"))
        - min(lengths[key] for key in ("long", "file", "input"))
        <= 1
    )
    assert {"x", "node", "y", "z"} <= set(result.terms)
    assert {item.source for item in result.exclusions} == {"long", "file", "input"}
    assert build_query(result.contributions).contributions == result.contributions


def test_query_tokenization_never_examines_more_than_the_shared_character_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    examined = []

    def counted_tokens(text, include_simple_identifiers=False):
        examined.append(len(text))
        return tokens(text, include_simple_identifiers)

    monkeypatch.setattr(memory_query, "tokens", counted_tokens)
    result = build_query(
        tuple(
            QueryContribution(kind=kind, source=kind, text="界 ＦｏｏBar " * 4000)
            for kind in ("literal", "identifier", "variable", "file", "input")
        )
    )

    assert sum(examined) == result.examined_chars == MAX_QUERY_CHARS
    assert len(result.terms) <= MAX_QUERY_TERMS
    assert set(result.identifiers) <= set(result.terms)
    assert {item.source for item in result.exclusions} == {
        "literal",
        "identifier",
        "variable",
        "file",
        "input",
    }


def test_query_character_budget_handles_more_sources_than_characters():
    result = build_query(
        tuple(
            QueryContribution(kind="input", source=str(index), text="!")
            for index in range(MAX_QUERY_CHARS + 2)
        )
    )

    assert result.terms == ()
    assert result.examined_chars == len(result.contributions) == MAX_QUERY_CHARS
    assert result.exclusions == (
        QueryExclusion(source="32000", reason="query character limit"),
        QueryExclusion(source="32001", reason="query character limit"),
    )
    reconstructed = build_query(result.contributions)
    assert reconstructed.contributions == result.contributions
    assert reconstructed.terms == result.terms
    assert reconstructed.examined_chars == result.examined_chars


def test_query_duplicate_unicode_terms_do_not_waste_shared_capacity():
    result = build_query(
        (
            QueryContribution(
                kind="literal",
                source="first",
                text="Straße " + " ".join(f"left{index}" for index in range(300)),
            ),
            QueryContribution(
                kind="file",
                source="second",
                text="STRASSE Ｓｔｒａße src/FooBar.py "
                + " ".join(f"left{index}" for index in range(300)),
            ),
        )
    )

    assert len(result.terms) == len(set(result.terms)) == MAX_QUERY_TERMS
    assert result.terms.count("strasse") == 1
    assert "src/foobar.py" in result.terms
    assert result.identifiers == ("src/foobar.py",)
    assert "foo" not in result.identifiers
    assert build_query(result.contributions).terms == result.terms


def test_query_budget_with_more_sources_than_terms_reports_each_omission():
    result = build_query(
        tuple(
            QueryContribution(kind="input", source=str(index), text=f"word{index}")
            for index in range(MAX_QUERY_TERMS + 2)
        )
    )

    assert result.terms == tuple(f"word{index}" for index in range(MAX_QUERY_TERMS))
    assert {item.source for item in result.exclusions} == {"256", "257"}
    assert all("term limit" in item.reason for item in result.exclusions)
    assert build_query(result.contributions).terms == result.terms


def test_query_small_inputs_keep_lexical_order_and_all_contribution_kinds():
    contributions = (
        QueryContribution(kind="file", source="file", text="Straße filetext"),
        QueryContribution(kind="literal", source="literal", text="The ReadFile"),
        QueryContribution(kind="identifier", source="node", text="build"),
        QueryContribution(kind="variable", source="goal", text="chosen"),
        QueryContribution(kind="input", source="input", text="inputtext STRASSE"),
    )
    excluded = (QueryExclusion(source="env:TOKEN", reason="environment excluded"),)
    result = build_query(contributions, excluded)

    assert result.terms == (
        "readfile",
        "read",
        "file",
        "build",
        "chosen",
        "strasse",
        "filetext",
        "inputtext",
    )
    assert result.identifiers == ("readfile", "build")
    assert result.exclusions == excluded
    assert result.examined_chars == sum(len(item.text) for item in contributions)
    assert {item.kind for item in result.contributions} == {
        "literal",
        "identifier",
        "variable",
        "file",
        "input",
    }
    assert build_query(()).terms == ()
    assert build_query(()).examined_chars == 0


def test_query_budget_policy_is_versioned_in_semantic_identity():
    selection = select_memory(MemorySnapshot(), "work", "executor", query("read"))

    assert selection.query.policy_revision == QUERY_REVISION
    assert selection.semantic_payload()["query"]["policy_revision"] == QUERY_REVISION
    payload = selection.model_dump(mode="json")
    payload["query"]["policy_revision"] = QUERY_REVISION + 1
    with pytest.raises(ValidationError, match="policy revision"):
        MemorySelection.model_validate(payload)


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


def test_exact_fit_keeps_ranked_omissions_and_record_limit_precedence():
    snapshot = accepted_snapshot(
        LessonProposal(title="src/file.py", body="x" * 9000),
        LessonProposal(title="File", body="Retain checks."),
        LessonProposal(title="File", body="y" * 9000),
        LessonProposal(title="Ocean", body="Inspect file."),
    )
    records = tuple(
        record.model_copy(update={"record_id": record_id})
        for record, record_id in zip(
            snapshot.records,
            ("a-oversized", "b-fit", "c-oversized", "d-body"),
            strict=True,
        )
    )
    expected_rendered = render_block((("b-fit", records[1].revisions[0]),))
    snapshot = MemorySnapshot(records=tuple(reversed(records)))
    before = snapshot.model_dump_json()

    result = select_memory(
        snapshot,
        "build",
        "reviewer",
        query("src/file.py"),
        max_records=1,
        max_chars=len(expected_rendered),
        append_after_fragment_count=3,
    )

    assert [item.record_id for item in result.selected] == ["b-fit"]
    assert result.rendered == expected_rendered
    assert result.rendered_chars == result.max_chars
    assert result.omissions == (
        "a-oversized: complete record does not fit character limit",
        "c-oversized: record limit",
        "d-body: record limit",
    )
    assert (result.node_id, result.role, result.append_after_fragment_count) == (
        "build",
        "reviewer",
        3,
    )
    assert snapshot.model_dump_json() == before


@pytest.mark.parametrize(
    "tamper,message",
    [
        ("query", "Memory query disagrees with its frozen eligible contributions."),
        ("policy", "Unsupported memory retrieval/rendering policy revision."),
        ("duplicate", "A memory record can be selected only once per role."),
        ("limit", "Memory selection exceeds recall limits."),
        (
            "rendering",
            "Memory selection rendering disagrees with complete frozen records.",
        ),
        (
            "score",
            "Selected memory must be an accepted matching revision with its lexical score.",
        ),
        (
            "disposition",
            "Selected memory must be an accepted matching revision with its lexical score.",
        ),
        ("order", "Memory selection is not in deterministic rank order."),
    ],
)
def test_frozen_selection_validation_preserves_error_precedence(tamper, message):
    selection = select_memory(
        accepted_snapshot(
            LessonProposal(title="Files", body="Protect reads."),
            LessonProposal(title="Reads", body="Protect files."),
        ),
        "build",
        "executor",
        query("files"),
    )
    payload = selection.model_dump(mode="json")
    payload["selected"].reverse()
    payload["rendered"] = render_block(
        tuple((item.record_id, item.revision) for item in reversed(selection.selected))
    )
    payload["rendered_chars"] = len(payload["rendered"])
    if tamper in {"query", "policy"}:
        payload["retrieval_revision"] += 1
    if tamper == "query":
        payload["query"]["terms"] = ["other"]
    if tamper in {"query", "policy", "duplicate"}:
        payload["selected"].append(payload["selected"][0])
    if tamper in {"query", "policy", "duplicate", "limit"}:
        payload["max_records"] = 1
    if tamper in {"query", "policy", "duplicate", "limit", "rendering"}:
        payload["rendered"] += "tampered"
    if tamper in {"rendering", "score"}:
        payload["selected"][0]["score"] = [0, 0, 0]
    if tamper == "disposition":
        payload["selected"][0]["revision"]["disposition"] = "pending"
        payload["selected"][0]["revision"]["acceptance"] = None

    with pytest.raises(ValidationError) as error:
        MemorySelection.model_validate(payload)

    assert [item["msg"] for item in error.value.errors()] == [f"Value error, {message}"]


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

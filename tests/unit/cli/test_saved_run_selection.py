from __future__ import annotations

from pathlib import Path

import pytest

from crewplane.artifacts.run_history import (
    RunHistoryDiagnostic,
    RunHistoryScan,
    scan_run_history,
)
from crewplane.cli.runs.selection import (
    RunSelectionError,
    resolve_run,
    run_selectors,
    warn_excluded_runs,
)
from tests.helpers.saved_runs import saved_run


def test_selector_precedence_and_deduplication(tmp_path: Path) -> None:
    older = saved_run(tmp_path, "id", "a-id", started_offset=0)
    newer = saved_run(tmp_path, "id-long", "id", started_offset=5)
    prefix = saved_run(tmp_path, "prefix-one", "prefix-key", started_offset=10)
    history = scan_run_history(tmp_path)
    history = RunHistoryScan(
        history.records,
        (
            RunHistoryDiagnostic(
                "id", tmp_path / "excluded/id", "invalid_json", "Invalid JSON."
            ),
        ),
    )
    assert resolve_run(history, "latest") == prefix
    assert resolve_run(history, "id") == newer
    assert resolve_run(history, "a-id") == older
    assert resolve_run(history, "id-long") == newer
    assert resolve_run(history, "prefix") == prefix


def test_exact_run_id_wins_over_other_prefix(tmp_path: Path) -> None:
    exact = saved_run(tmp_path, "short", "a-run")
    saved_run(tmp_path, "short-more", "short-key", started_offset=1)
    assert resolve_run(scan_run_history(tmp_path), "short") == exact


def test_duplicate_ids_and_prefixes_are_never_guessed(tmp_path: Path) -> None:
    first = saved_run(tmp_path, "shared", "one-key", workflow_name="One")
    second = saved_run(
        tmp_path, "shared", "two-key", workflow_name="Two", started_offset=1
    )
    prefix = saved_run(
        tmp_path, "shared-more", "three-key", workflow_name="Three", started_offset=2
    )
    history = scan_run_history(tmp_path)
    with pytest.raises(RunSelectionError) as caught:
        resolve_run(history, "shared")
    assert str(caught.value) == (
        "Ambiguous saved run 'shared'; use an exact full key:\n"
        f"  Two | {second.manifest.started_at} | two-key\n"
        f"  One | {first.manifest.started_at} | one-key"
    )
    assert list(run_selectors(history).items()) == [
        ("three-key", "shared-more"),
        ("two-key", "two-key"),
        ("one-key", "one-key"),
    ]
    assert resolve_run(history, "one-key") == first
    assert resolve_run(history, "two-key") == second
    with pytest.raises(RunSelectionError) as caught:
        resolve_run(history, "sha")
    assert str(caught.value) == (
        "Ambiguous saved run 'sha'; use an exact full key:\n"
        f"  Three | {prefix.manifest.started_at} | three-key\n"
        f"  Two | {second.manifest.started_at} | two-key\n"
        f"  One | {first.manifest.started_at} | one-key"
    )


@pytest.mark.parametrize(
    "selector", ["", " ", ".", "..", "../a", "a/b", r"a\b", "unknown"]
)
def test_invalid_or_unmatched_selector(tmp_path: Path, selector: str) -> None:
    history = scan_run_history(tmp_path)
    with pytest.raises(RunSelectionError) as caught:
        resolve_run(history, selector)
    assert str(caught.value) == (
        "No saved execution run matches 'unknown'. Use crewplane runs list."
        if selector == "unknown"
        else "RUN must be a nonempty identifier, not a path. Use crewplane runs list."
    )


def test_excluded_exact_key_reports_diagnostic(tmp_path: Path) -> None:
    record = saved_run(tmp_path)
    saved_run(tmp_path, record.manifest.run_key_name, "matching-prefix-key")
    record.manifest_path.write_bytes(b"not JSON")
    with pytest.raises(RunSelectionError) as caught:
        resolve_run(scan_run_history(tmp_path), record.manifest.run_key_name)
    assert str(caught.value) == (
        f"Saved run {record.manifest.run_key_name!r} was excluded (invalid_json): "
        "Execution manifest is not valid JSON. "
        f"Path: {record.manifest_path}"
    )


def test_empty_latest_and_unique_display_id(tmp_path: Path) -> None:
    with pytest.raises(RunSelectionError) as caught:
        resolve_run(RunHistoryScan((), ()), "latest")
    assert str(caught.value) == (
        "No saved execution runs found. Use crewplane runs list."
    )
    record = saved_run(tmp_path)
    assert run_selectors(scan_run_history(tmp_path)) == {
        record.manifest.run_key_name: "run-1"
    }


@pytest.mark.parametrize("selector", [" run-1", "run-1 "])
def test_selector_whitespace_is_not_normalized(tmp_path: Path, selector: str) -> None:
    saved_run(tmp_path)
    with pytest.raises(RunSelectionError) as caught:
        resolve_run(scan_run_history(tmp_path), selector)
    assert str(caught.value) == (
        f"No saved execution run matches {selector!r}. Use crewplane runs list."
    )


def test_latest_precedes_an_exact_key(tmp_path: Path) -> None:
    saved_run(tmp_path, "old", "latest")
    newer = saved_run(tmp_path, "new", "new-key", started_offset=1)
    assert resolve_run(scan_run_history(tmp_path), "latest") == newer


def test_exclusion_warnings_aggregate_missing_manifests_before_other_diagnostics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    diagnostics = (
        RunHistoryDiagnostic("z-key", tmp_path / "z", "invalid_json", "Invalid JSON."),
        RunHistoryDiagnostic("missing-1", tmp_path / "m1", "no_manifest", "Missing."),
        RunHistoryDiagnostic("a-key", tmp_path / "a", "key_mismatch", "Wrong key."),
        RunHistoryDiagnostic("missing-2", tmp_path / "m2", "no_manifest", "Missing."),
    )

    warn_excluded_runs(RunHistoryScan((), diagnostics))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "Excluded 2 directory(s) without execution manifests; "
        "these may contain preflight or incomplete attempts.\n"
        f"Excluded 'z-key' (invalid_json): Invalid JSON. Path: {tmp_path / 'z'}\n"
        f"Excluded 'a-key' (key_mismatch): Wrong key. Path: {tmp_path / 'a'}\n"
    )

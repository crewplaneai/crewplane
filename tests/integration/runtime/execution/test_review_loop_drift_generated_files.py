from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from crewplane.artifacts.generated_files.catalog import (
    generated_file_source_root,
)
from crewplane.runtime.execution.review_loop.drift import (
    capture as review_loop_drift_capture,
)
from crewplane.runtime.execution.review_loop.drift import (
    detection as review_loop_drift_detection,
)
from crewplane.runtime.execution.review_loop.drift import (
    guard as review_loop_drift_guard,
)
from crewplane.runtime.execution.review_loop.drift import (
    node as review_loop_drift_node,
)
from crewplane.runtime.execution.review_loop.drift import (
    snapshots as review_loop_drift_snapshots,
)
from crewplane.runtime.execution.review_loop.types import (
    ActivityWindow,
    DriftMonitoringWindow,
)
from tests.integration.runtime.execution.review_loop_drift_support import (
    make_drift_request,
)


def test_runtime_generated_file_source_snapshots_are_allowed(
    tmp_path: Path,
) -> None:
    request, _output, _node_dir = make_drift_request(tmp_path)
    snapshot_root = generated_file_source_root(request.output_file)
    generated_source = snapshot_root / "src/app.txt"
    generated_source.parent.mkdir(parents=True)
    generated_source.write_text("generated", encoding="utf-8")
    allowance = review_loop_drift_guard.GeneratedFileDriftAllowance()
    allowance.start_snapshot(snapshot_root)
    allowance.finish_snapshot(
        snapshot_root,
        {
            generated_source: (
                review_loop_drift_snapshots.file_snapshot_signature(generated_source)
            )
        },
    )
    request.generated_file_allowance = allowance
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot=None,
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=None),
    )

    drift = review_loop_drift_detection.detect_provider_call_drift(
        request,
        window,
        event_log_capture=None,
        event_log_start_index=0,
    )

    assert drift.warning_paths == ()
    assert drift.fatal_paths == ()


def test_unregistered_generated_file_source_snapshots_are_drift(
    tmp_path: Path,
) -> None:
    request, _output, _node_dir = make_drift_request(tmp_path)
    generated_source = generated_file_source_root(request.output_file) / "src/app.txt"
    generated_source.parent.mkdir(parents=True)
    generated_source.write_text("generated", encoding="utf-8")
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot=None,
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=None),
    )

    drift = review_loop_drift_detection.detect_provider_call_drift(
        request,
        window,
        event_log_capture=None,
        event_log_start_index=0,
    )

    assert generated_source not in request.allowed_paths
    assert drift.warning_paths == (generated_source,)
    assert drift.fatal_paths == ()


def test_completed_generated_file_snapshot_allows_only_published_files(
    tmp_path: Path,
) -> None:
    request, _output, _node_dir = make_drift_request(tmp_path)
    request.drift_session = review_loop_drift_guard.create_drift_guard_session(None)
    snapshot_root = generated_file_source_root(request.output_file)
    published_source = snapshot_root / "src/app.txt"
    allowance = request.drift_session.generated_file_allowance
    allowance.start_snapshot(snapshot_root)
    published_source.parent.mkdir(parents=True)
    published_source.write_text("generated", encoding="utf-8")
    allowance.finish_snapshot(
        snapshot_root,
        {
            published_source: (
                review_loop_drift_snapshots.file_snapshot_signature(published_source)
            )
        },
    )

    unexpected_source = snapshot_root / "src/unregistered.txt"
    unexpected_source.write_text("unexpected", encoding="utf-8")
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot=None,
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=None),
    )

    drift = review_loop_drift_detection.detect_provider_call_drift(
        request,
        window,
        event_log_capture=None,
        event_log_start_index=0,
    )

    assert drift.warning_paths == (unexpected_source,)
    assert drift.fatal_paths == ()


@pytest.mark.parametrize(
    ("relative_path", "mutation"),
    [
        ("generated-files.json", "rewrite"),
        ("generated-files.json", "delete"),
        ("src/app.txt", "rewrite"),
        ("src/app.txt", "delete"),
    ],
)
def test_completed_generated_file_snapshot_detects_publication_drift(
    tmp_path: Path,
    relative_path: str,
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _output, _node_dir = make_drift_request(tmp_path)
    request.drift_session = review_loop_drift_guard.create_drift_guard_session(None)
    snapshot_root = generated_file_source_root(request.output_file)
    published_path = snapshot_root / relative_path
    published_path.parent.mkdir(parents=True, exist_ok=True)
    published_path.write_text("runtime publication", encoding="utf-8")
    allowance = request.drift_session.generated_file_allowance
    allowance.start_snapshot(snapshot_root)
    allowance.finish_snapshot(
        snapshot_root,
        {
            published_path: (
                review_loop_drift_snapshots.file_snapshot_signature(published_path)
            )
        },
    )
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot=None,
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=None),
    )
    scan_started = Event()
    mutation_complete = Event()
    original_snapshot_files = review_loop_drift_snapshots.snapshot_files

    def snapshot_after_concurrent_mutation(
        root: Path,
        excluded_paths: set[Path] | None = None,
        excluded_roots: set[Path] | None = None,
    ) -> dict[Path, tuple[int, str]]:
        if root == request.node_dir:
            scan_started.set()
            assert mutation_complete.wait(timeout=5)
        return original_snapshot_files(root, excluded_paths, excluded_roots)

    monkeypatch.setattr(
        review_loop_drift_node,
        "snapshot_files",
        snapshot_after_concurrent_mutation,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        drift_future = executor.submit(
            review_loop_drift_detection.detect_provider_call_drift,
            request,
            window,
            None,
            0,
        )
        assert scan_started.wait(timeout=5)
        if mutation == "rewrite":
            published_path.write_text("other reviewer", encoding="utf-8")
        else:
            published_path.unlink()
        mutation_complete.set()
        drift = drift_future.result(timeout=5)

    assert drift.warning_paths == (published_path,)
    assert drift.fatal_paths == ()


def test_generated_file_snapshot_published_during_node_scan_is_allowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _output, _node_dir = make_drift_request(tmp_path)
    request.drift_session = review_loop_drift_guard.create_drift_guard_session(None)
    snapshot_root = generated_file_source_root(request.output_file)
    generated_source = snapshot_root / "src/app.txt"
    unexpected_source = snapshot_root / "src/unregistered.txt"
    allowance = request.drift_session.generated_file_allowance
    original_snapshot_files = review_loop_drift_snapshots.snapshot_files

    published = False
    node_scan_count = 0

    def publish_generated_file_after_scan(
        root: Path,
        excluded_paths: set[Path] | None = None,
        excluded_roots: set[Path] | None = None,
    ) -> dict[Path, tuple[int, str]]:
        nonlocal node_scan_count, published
        snapshot = original_snapshot_files(root, excluded_paths, excluded_roots)
        if root != request.node_dir or published:
            if root == request.node_dir:
                node_scan_count += 1
            return snapshot
        node_scan_count += 1
        published = True
        allowance.start_snapshot(snapshot_root)
        generated_source.parent.mkdir(parents=True)
        generated_source.write_text("generated", encoding="utf-8")
        unexpected_source.write_text("unexpected", encoding="utf-8")
        allowance.finish_snapshot(
            snapshot_root,
            {
                generated_source: (
                    review_loop_drift_snapshots.file_snapshot_signature(
                        generated_source
                    )
                )
            },
        )
        return snapshot

    monkeypatch.setattr(
        review_loop_drift_node,
        "snapshot_files",
        publish_generated_file_after_scan,
    )
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot=None,
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=None),
    )

    drift = review_loop_drift_node.detect_node_drift(request, window)

    assert drift.warning_paths == (unexpected_source,)
    assert drift.fatal_paths == ()
    assert node_scan_count == 2


def test_in_progress_generated_file_root_is_not_read_during_node_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _output, _node_dir = make_drift_request(tmp_path)
    request.drift_session = review_loop_drift_guard.create_drift_guard_session(None)
    snapshot_root = generated_file_source_root(request.output_file)
    changing_file = snapshot_root / "src/changing.bin"
    changing_file.parent.mkdir(parents=True)
    changing_file.write_bytes(b"runtime write")
    allowance = request.drift_session.generated_file_allowance
    allowance.start_snapshot(snapshot_root)
    allowance.finish_snapshot(
        snapshot_root,
        {
            changing_file: (
                review_loop_drift_snapshots.file_snapshot_signature(changing_file)
            )
        },
    )
    allowance.start_snapshot(snapshot_root)
    original_read_bytes = Path.read_bytes
    read_count = 0

    def append_after_each_read(path: Path) -> bytes:
        nonlocal read_count
        payload = original_read_bytes(path)
        if path == changing_file:
            read_count += 1
            with path.open("ab") as stream:
                stream.write(b"x")
        return payload

    monkeypatch.setattr(Path, "read_bytes", append_after_each_read)
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot=None,
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=None),
    )

    drift = review_loop_drift_node.detect_node_drift(request, window)

    assert drift.warning_paths == ()
    assert drift.fatal_paths == ()
    assert read_count == 0


def test_peer_runtime_owned_file_does_not_create_directory_drift(
    tmp_path: Path,
) -> None:
    request, _output, node_dir = make_drift_request(tmp_path)
    peer_state = node_dir / "workspace-state" / "peer.json"
    request.runtime_owned_paths.add(peer_state)
    window = review_loop_drift_capture.capture_drift_monitoring_window(
        node_id=request.node.id,
        node_dir=node_dir,
        output=request.output,
        telemetry=None,
        runtime_publications=request.runtime_context.runtime_publications,
        runtime_owned_paths=request.runtime_owned_paths,
    )

    peer_state.parent.mkdir()
    peer_state.write_text("runtime state", encoding="utf-8")

    drift = review_loop_drift_node.detect_node_drift(request, window)

    assert drift.warning_paths == ()
    assert drift.fatal_paths == ()


def test_failed_generated_file_snapshot_does_not_allow_partial_files(
    tmp_path: Path,
) -> None:
    request, _output, _node_dir = make_drift_request(tmp_path)
    request.drift_session = review_loop_drift_guard.create_drift_guard_session(None)
    snapshot_root = generated_file_source_root(request.output_file)
    partial_source = snapshot_root / "src/partial.txt"
    allowance = request.drift_session.generated_file_allowance
    allowance.start_snapshot(snapshot_root)
    partial_source.parent.mkdir(parents=True)
    partial_source.write_text("partial", encoding="utf-8")
    allowance.finish_snapshot(snapshot_root, None)
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot=None,
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=None),
    )

    drift = review_loop_drift_detection.detect_provider_call_drift(
        request,
        window,
        event_log_capture=None,
        event_log_start_index=0,
    )

    assert drift.warning_paths == (partial_source,)
    assert drift.fatal_paths == ()

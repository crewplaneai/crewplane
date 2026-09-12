import hashlib
import os
from pathlib import Path

import pytest

from crewplane.runtime.execution.review_loop.drift import (
    capture as review_loop_drift_capture,
)
from crewplane.runtime.execution.review_loop.drift import (
    detection as review_loop_drift_detection,
)
from crewplane.runtime.execution.review_loop.drift import (
    recovery as review_loop_drift_recovery,
)
from crewplane.runtime.execution.review_loop.drift import (
    snapshots as review_loop_drift_snapshots,
)
from tests.integration.runtime.execution.review_loop_drift_support import (
    make_drift_request,
    recovery_payload,
)


def test_monitoring_window_retries_concurrent_runtime_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, output, node_dir = make_drift_request(tmp_path)
    publications = request.runtime_context.runtime_publications
    result_path = output.results_dir / "peer-result.md"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_bytes(b"original result")
    replacement_path = tmp_path / "peer-result-replacement.md"
    replacement_payload = b"published peer result"
    replacement_signature = (
        len(replacement_payload),
        hashlib.sha256(replacement_payload).hexdigest(),
    )
    original_read_bytes = Path.read_bytes
    publication_count = 0

    def publish_during_read(path: Path) -> bytes:
        nonlocal publication_count
        payload = original_read_bytes(path)
        if path == result_path and publication_count == 0:
            replacement_path.write_bytes(replacement_payload)
            with publications.transaction():
                os.replace(replacement_path, result_path)
                publications.publish(
                    result_path,
                    replacement_signature,
                    recovery_source=result_path,
                )
            publication_count += 1
        return payload

    monkeypatch.setattr(
        Path,
        "read_bytes",
        publish_during_read,
    )

    window = review_loop_drift_capture.capture_drift_monitoring_window(
        request.node.id,
        node_dir,
        output,
        telemetry=None,
        runtime_publications=publications,
    )

    assert publication_count == 1
    assert window.shared_reserved_original_bytes == {}
    assert window.shared_reserved_snapshot[result_path] == replacement_signature
    assert recovery_payload(publications, result_path) == replacement_payload
    publications.close()


def test_monitoring_windows_share_disk_backed_recovery_baseline(
    tmp_path: Path,
) -> None:
    request, output, node_dir = make_drift_request(tmp_path)
    result_path = output.results_dir / "peer-result.md"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_bytes(b"shared recovery payload")
    recovery_baseline = review_loop_drift_capture.capture_drift_recovery_baseline(
        node_dir,
        output,
        request.runtime_context.runtime_publications,
    )

    first = review_loop_drift_capture.capture_drift_monitoring_window(
        request.node.id,
        node_dir,
        output,
        telemetry=None,
        runtime_publications=request.runtime_context.runtime_publications,
        recovery_baseline=recovery_baseline,
    )
    second = review_loop_drift_capture.capture_drift_monitoring_window(
        request.node.id,
        node_dir,
        output,
        telemetry=None,
        runtime_publications=request.runtime_context.runtime_publications,
        recovery_baseline=recovery_baseline,
    )

    assert first.node_original_bytes is second.node_original_bytes
    assert first.shared_reserved_original_bytes is second.shared_reserved_original_bytes
    assert first.node_original_bytes == {}
    assert first.shared_reserved_original_bytes == {}
    assert (
        recovery_payload(
            request.runtime_context.runtime_publications,
            result_path,
        )
        == b"shared recovery payload"
    )
    request.runtime_context.runtime_publications.close()


def test_recovery_baseline_without_registry_retains_original_bytes(
    tmp_path: Path,
) -> None:
    request, output, node_dir = make_drift_request(tmp_path)
    result_path = output.results_dir / "peer-result.md"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_bytes(b"in-memory fallback")

    recovery_baseline = review_loop_drift_capture.capture_drift_recovery_baseline(
        node_dir,
        output,
    )

    assert recovery_baseline.shared_reserved_original_bytes == {
        result_path: b"in-memory fallback"
    }
    assert recovery_baseline.shared_reserved_snapshot[result_path] == (
        len(b"in-memory fallback"),
        hashlib.sha256(b"in-memory fallback").hexdigest(),
    )
    request.runtime_context.runtime_publications.close()


def test_registered_reserved_publication_created_during_window_is_restored(
    tmp_path: Path,
) -> None:
    request, output, node_dir = make_drift_request(tmp_path)
    publications = request.runtime_context.runtime_publications
    request.runtime_publications = publications
    window = review_loop_drift_capture.capture_drift_monitoring_window(
        request.node.id,
        node_dir,
        output,
        telemetry=None,
        runtime_publications=publications,
    )
    result_path = output.results_dir / "sibling-result.md"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    trusted_payload = b"trusted sibling publication"
    result_path.write_bytes(trusted_payload)
    publications.publish(
        result_path,
        review_loop_drift_snapshots.file_snapshot_signature(result_path),
        recovery_source=result_path,
    )
    result_path.write_bytes(b"provider mutation")

    drift = review_loop_drift_detection.detect_provider_call_drift(
        request,
        window,
        event_log_capture=None,
        event_log_start_index=0,
    )
    review_loop_drift_recovery.restore_fatal_artifacts(
        request,
        window,
        drift.fatal_paths,
    )

    assert result_path in drift.fatal_paths
    assert result_path.read_bytes() == trusted_payload
    publications.close()


@pytest.mark.parametrize("with_registry", [False, True])
def test_reserved_scope_covers_roots_and_excludes_only_shared_log_artifacts(
    tmp_path, with_registry
) -> None:
    request, output, node_dir = make_drift_request(tmp_path)
    publications = request.runtime_context.runtime_publications
    roots = (
        output.results_dir,
        output.stages_dir / "manifests",
        output.get_run_log_dir(),
    )
    expected = {}
    directories = set()
    for root in roots:
        nested = root / "nested"
        nested.mkdir(parents=True, exist_ok=True)
        directories.update((root, nested))
        for name in ("record", "events.ndjson", "summary.md"):
            path = nested / name
            path.write_bytes(b"nested")
            expected[path] = b"nested"
        path = root / "record"
        path.write_bytes(b"reserved")
        expected[path] = b"reserved"
    output.get_run_event_log_path().write_bytes(b"events")
    output.get_run_summary_path().write_bytes(b"summary")
    try:
        baseline = review_loop_drift_capture.capture_drift_recovery_baseline(
            node_dir, output, publications if with_registry else None
        )
        assert baseline.shared_reserved_snapshot == {
            path: (len(payload), hashlib.sha256(payload).hexdigest())
            for path, payload in expected.items()
        }
        assert set(baseline.shared_reserved_directory_snapshot) == directories
        assert baseline.shared_reserved_original_bytes == (
            {} if with_registry else expected
        )
        if with_registry:
            for path, payload in expected.items():
                assert recovery_payload(publications, path) == payload
        window = review_loop_drift_capture.capture_drift_monitoring_window(
            request.node.id, node_dir, output, None, recovery_baseline=baseline
        )
        assert window.event_log_before == b"events"
        assert window.summary_before == b"summary"
    finally:
        publications.close()


@pytest.mark.parametrize(
    "scan_name",
    [
        "shared_reserved_snapshot",
        "shared_reserved_original_bytes",
        "shared_reserved_directory_snapshot",
    ],
)
def test_reserved_scans_preserve_missing_roots_and_unsafe_entries(
    tmp_path, scan_name
) -> None:
    request, output, _node_dir = make_drift_request(tmp_path)
    scan = getattr(review_loop_drift_capture, scan_name)
    manifests = output.stages_dir / "manifests"
    try:
        initial = scan(output)
        assert not any(
            path == manifests or path.is_relative_to(manifests) for path in initial
        )
        manifests.mkdir()
        safe = manifests / "safe"
        safe.write_bytes(b"safe")
        hardlink = manifests / "hardlink"
        hardlink.hardlink_to(safe)
        symlink = manifests / "symlink"
        symlink.symlink_to(tmp_path / "outside")
        snapshot = scan(output)
        if scan_name == "shared_reserved_snapshot":
            assert {safe, hardlink, symlink} <= snapshot.keys()
        elif scan_name == "shared_reserved_original_bytes":
            assert snapshot == {}
        else:
            assert manifests in snapshot
            assert not {safe, hardlink, symlink} & snapshot.keys()
    finally:
        request.runtime_context.runtime_publications.close()

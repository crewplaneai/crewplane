from __future__ import annotations

import json
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.artifacts.generated_files import snapshot_metadata
from crewplane.artifacts.generated_files.paths import (
    GENERATED_FILE_SNAPSHOT_METADATA_NAME,
    GENERATED_FILE_SOURCE_METADATA_NAME,
)
from crewplane.artifacts.generated_files.snapshot_policy import (
    GeneratedFileRejectionLog,
)


@pytest.mark.parametrize("payload", [None, b"\xff", b"[]", b"{}"])
def test_readers_preserve_missing_and_invalid_metadata_defaults(
    tmp_path: Path, payload: bytes | None
) -> None:
    if payload is not None:
        for name in (
            GENERATED_FILE_SOURCE_METADATA_NAME,
            GENERATED_FILE_SNAPSHOT_METADATA_NAME,
        ):
            (tmp_path / name).write_bytes(payload)

    assert snapshot_metadata.read_snapshot_source_root(tmp_path) is None
    assert snapshot_metadata.read_snapshot_candidate_files(tmp_path) == (
        None if payload is None else ()
    )
    assert snapshot_metadata.generated_file_snapshot_rejection_summary(tmp_path) == (
        snapshot_metadata.GeneratedFileSnapshotRejectionSummary()
    )


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_readers_preserve_paths_entry_order_and_duplicates(
    tmp_path: Path, encoding: str
) -> None:
    first = tmp_path / "z.txt"
    second = tmp_path / "a.txt"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    source_root = Path("missing-source") / "café"
    (tmp_path / GENERATED_FILE_SOURCE_METADATA_NAME).write_bytes(
        json.dumps({"source_root": source_root.as_posix()}).encode(encoding)
    )
    (tmp_path / GENERATED_FILE_SNAPSHOT_METADATA_NAME).write_bytes(
        json.dumps(
            {
                "files": [
                    {"path": "z.txt"},
                    None,
                    {"path": "../outside.txt"},
                    {"path": 4},
                    {"path": "a.txt"},
                    {"path": "missing.txt"},
                    {"path": "z.txt"},
                ]
            }
        ).encode(encoding)
    )

    assert snapshot_metadata.read_snapshot_source_root(tmp_path) == source_root
    assert snapshot_metadata.read_snapshot_candidate_files(tmp_path) == (
        first,
        second,
        first,
    )


@pytest.mark.parametrize(
    ("reader", "metadata_name", "default"),
    [
        (
            snapshot_metadata.read_snapshot_source_root,
            GENERATED_FILE_SOURCE_METADATA_NAME,
            None,
        ),
        (
            snapshot_metadata.read_snapshot_candidate_files,
            GENERATED_FILE_SNAPSHOT_METADATA_NAME,
            (),
        ),
        (
            snapshot_metadata.generated_file_snapshot_rejection_summary,
            GENERATED_FILE_SNAPSHOT_METADATA_NAME,
            snapshot_metadata.GeneratedFileSnapshotRejectionSummary(),
        ),
    ],
    ids=["source", "candidates", "rejections"],
)
@pytest.mark.parametrize(
    "operation", ["contained_regular_file", "read_contained_bytes"]
)
@pytest.mark.parametrize("error_type", [OSError, ValueError, RuntimeError])
def test_reader_exception_boundaries(
    tmp_path: Path,
    reader: Callable[[Path], object],
    metadata_name: str,
    default: object,
    operation: str,
    error_type: type[Exception],
) -> None:
    (tmp_path / metadata_name).write_bytes(b"{}")
    failure = error_type("metadata failure")

    with patch.object(snapshot_metadata, operation, side_effect=failure):
        if operation == "read_contained_bytes" and error_type in (OSError, ValueError):
            assert reader(tmp_path) == default
        else:
            with pytest.raises(error_type) as raised:
                reader(tmp_path)
            assert raised.value is failure


@pytest.mark.parametrize(
    ("raw_count", "expected_count"),
    [(None, 2), (True, 2), (-1, 2), (1, 2), ("5", 2), (2, 2), (5, 5)],
)
def test_rejection_summary_preserves_details_and_normalizes_counts(
    tmp_path: Path, raw_count: object, expected_count: int
) -> None:
    details = [{"path": "z.txt"}, {"path": "a.txt"}]
    (tmp_path / GENERATED_FILE_SNAPSHOT_METADATA_NAME).write_bytes(
        json.dumps(
            {
                "rejected_files": [details[0], None, details[1]],
                "rejected_file_count": raw_count,
                "rejected_files_truncated": False,
            }
        ).encode("utf-8")
    )

    summary = snapshot_metadata.generated_file_snapshot_rejection_summary(tmp_path)

    assert summary.recorded_files == tuple(details)
    assert summary.total_count == expected_count
    assert summary.truncated is (expected_count > len(details))


def test_source_writer_preserves_exact_serialization_and_creates_parent(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot"
    expected = b'{"source_root": "caf\\u00e9"}\n'

    signature = snapshot_metadata.write_source_metadata(snapshot, Path("café"))

    assert (snapshot / GENERATED_FILE_SOURCE_METADATA_NAME).read_bytes() == expected
    assert signature == (len(expected), sha256(expected).hexdigest())
    assert sorted(path.name for path in snapshot.iterdir()) == [
        GENERATED_FILE_SOURCE_METADATA_NAME
    ]


@pytest.mark.parametrize("rejected_count", [0, 2])
def test_snapshot_writer_preserves_exact_bytes_order_and_rejection_log(
    tmp_path: Path, rejected_count: int
) -> None:
    rejections = GeneratedFileRejectionLog(1)
    for index in range(rejected_count):
        rejections.record(
            {
                "path": f"{index}.txt",
                "size_bytes": 1,
                "discovery_source": "baseline",
                "explicit": False,
                "disposition": "rejected",
                "reason": "file_count_limit",
            }
        )
    details_before = list(rejections.rejected_files)
    metadata = [
        {"path": "z.txt", "changed": None, "size_bytes": 2},
        {"path": "a.txt", "changed": True, "size_bytes": 1},
    ]
    expected = (
        b'{"files": [{"changed": null, "path": "z.txt", "size_bytes": 2}, '
        b'{"changed": true, "path": "a.txt", "size_bytes": 1}]'
    )
    if rejected_count:
        expected += (
            b', "rejected_file_count": 2, "rejected_files": '
            b'[{"discovery_source": "baseline", "disposition": "rejected", '
            b'"explicit": false, "path": "0.txt", "reason": "file_count_limit", '
            b'"size_bytes": 1}], "rejected_files_truncated": true'
        )
    expected += b"}\n"
    target = tmp_path / GENERATED_FILE_SNAPSHOT_METADATA_NAME
    target.write_bytes(b"previous metadata")

    signature = snapshot_metadata.write_snapshot_metadata(
        tmp_path, metadata, rejections
    )

    assert target.read_bytes() == expected
    assert signature == (len(expected), sha256(expected).hexdigest())
    assert rejections.rejected_file_count == rejected_count
    assert rejections.rejected_files == details_before
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("kind", ["source", "snapshot"])
def test_writer_propagates_publication_failure_and_cleans_temporary_file(
    tmp_path: Path, kind: str
) -> None:
    metadata_name = (
        GENERATED_FILE_SOURCE_METADATA_NAME
        if kind == "source"
        else GENERATED_FILE_SNAPSHOT_METADATA_NAME
    )
    target = tmp_path / metadata_name
    target.write_bytes(b"previous metadata")
    failure = OSError("sync failed")

    with (
        patch("crewplane.artifacts.atomic.os.fsync", side_effect=failure),
        pytest.raises(OSError) as raised,
    ):
        if kind == "source":
            snapshot_metadata.write_source_metadata(tmp_path, Path("source"))
        else:
            snapshot_metadata.write_snapshot_metadata(
                tmp_path, (), GeneratedFileRejectionLog(0)
            )

    assert raised.value is failure
    assert target.read_bytes() == b"previous metadata"
    assert list(tmp_path.iterdir()) == [target]


def test_snapshot_writer_rejects_nonfinite_values_before_publication(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot"

    with pytest.raises(ValueError, match="Out of range float values"):
        snapshot_metadata.write_snapshot_metadata(
            snapshot,
            [{"path": "bad.txt", "changed": None, "size_bytes": float("nan")}],
            GeneratedFileRejectionLog(0),
        )

    assert not snapshot.exists()

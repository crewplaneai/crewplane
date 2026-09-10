import hashlib
import os
import subprocess
import sys
import tempfile
import textwrap
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.runtime.execution.provider_call import (
    provider_output as provider_call_output,
)
from crewplane.runtime.execution.provider_call import publish_invocation_output
from crewplane.runtime.execution.publication_registry import (
    RuntimePublicationRegistry,
)


def _recovery_payload(
    registry: RuntimePublicationRegistry,
    path: Path,
) -> bytes | None:
    destination = BytesIO()
    if not registry.copy_recovery_payload_to(path, destination):
        return None
    return destination.getvalue()


def test_invocation_output_publication_stages_on_destination_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = tmp_path / "private"
    destination_dir = tmp_path / "node"
    source_dir.mkdir()
    destination_dir.mkdir()
    source = source_dir / "provider-output.md"
    destination = destination_dir / "reviewer.md"
    source.write_text("trusted reviewer output", encoding="utf-8")
    expected_signature = (
        23,
        "8073e76391e728426a7dfcd9c08af6dad50d89e69bdb982390c3f726fa995b31",
    )
    original_replace = provider_call_output.replace_contained_file
    staged_sources: list[Path] = []

    def record_replace(root: Path, relative_path: str, staged_source: Path) -> Path:
        staged_sources.append(staged_source)
        assert staged_source.parent == destination_dir
        return original_replace(root, relative_path, staged_source)

    monkeypatch.setattr(
        provider_call_output,
        "replace_contained_file",
        record_replace,
    )

    publications = RuntimePublicationRegistry()
    signature = publish_invocation_output(
        source,
        destination,
        publications,
        expected_signature,
    )

    assert signature == expected_signature
    assert destination.read_text(encoding="utf-8") == "trusted reviewer output"
    assert source.exists()
    assert len(staged_sources) == 1
    assert not staged_sources[0].exists()
    assert _recovery_payload(publications, destination) == b"trusted reviewer output"
    publications.close()


def test_runtime_publication_registry_validates_optional_recovery_source(
    tmp_path: Path,
) -> None:
    registry = RuntimePublicationRegistry()
    publication_path = tmp_path / "result.md"
    recovery_source = tmp_path / "trusted-result.md"
    payload = b"trusted result"
    signature = (len(payload), hashlib.sha256(payload).hexdigest())
    recovery_source.write_bytes(payload)

    registry.publish(publication_path, signature, recovery_source=recovery_source)

    assert _recovery_payload(registry, publication_path) == payload
    recovery_source.write_bytes(b"forged")
    with pytest.raises(ValueError, match="recovery source does not match"):
        registry.publish(
            publication_path,
            signature,
            recovery_source=recovery_source,
        )
    assert _recovery_payload(registry, publication_path) == payload

    second_path = tmp_path / "findings.md"
    second_source = tmp_path / "trusted-findings.md"
    second_payload = b"trusted findings"
    second_source.write_bytes(second_payload)
    second_signature = (
        len(second_payload),
        hashlib.sha256(second_payload).hexdigest(),
    )
    registry.publish(second_path, second_signature, recovery_source=second_source)
    replacement_payload = b"updated trusted result"
    recovery_source.write_bytes(replacement_payload)
    replacement_signature = (
        len(replacement_payload),
        hashlib.sha256(replacement_payload).hexdigest(),
    )
    registry.publish(
        publication_path,
        replacement_signature,
        recovery_source=recovery_source,
    )

    assert _recovery_payload(registry, publication_path) == replacement_payload
    assert _recovery_payload(registry, second_path) == second_payload

    registry.publish(publication_path, replacement_signature)

    assert _recovery_payload(registry, publication_path) is None
    registry.close()


def test_recovery_snapshot_does_not_register_or_duplicate_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_temporary_file = tempfile.TemporaryFile
    recovery_files: list[BinaryIO] = []

    def record_temporary_file(mode: str = "w+b") -> BinaryIO:
        recovery_file = cast(BinaryIO, real_temporary_file(mode=mode))
        recovery_files.append(recovery_file)
        return recovery_file

    monkeypatch.setattr(tempfile, "TemporaryFile", record_temporary_file)
    source = tmp_path / "baseline.md"
    source.write_bytes(b"baseline")
    signature = file_size_and_sha256(source)
    registry = RuntimePublicationRegistry()

    registry.capture_recovery_snapshot(source, signature)
    registry.capture_recovery_snapshot(source, signature)

    assert registry.snapshot() == ({}, 0)
    assert len(recovery_files) == 1
    assert os.fstat(recovery_files[0].fileno()).st_size == len(b"baseline")
    assert _recovery_payload(registry, source) == b"baseline"
    registry.close()


def test_large_runtime_recovery_is_disk_backed_and_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_temporary_file = tempfile.TemporaryFile
    recovery_files: list[BinaryIO] = []

    def record_temporary_file(mode: str = "w+b") -> BinaryIO:
        recovery_file = cast(BinaryIO, real_temporary_file(mode=mode))
        recovery_files.append(recovery_file)
        return recovery_file

    monkeypatch.setattr(
        tempfile,
        "TemporaryFile",
        record_temporary_file,
    )
    source = tmp_path / "large-result.md"
    payload_size = 16 * 1024 * 1024
    with source.open("wb") as source_file:
        chunk = b"x" * 1024 * 1024
        remaining_bytes = payload_size
        while remaining_bytes:
            source_file.write(chunk)
            remaining_bytes -= len(chunk)
    signature = file_size_and_sha256(source)
    registry = RuntimePublicationRegistry()

    registry.publish(source, signature, recovery_source=source)

    assert len(recovery_files) == 1
    assert os.fstat(recovery_files[0].fileno()).st_size == payload_size
    assert not recovery_files[0].closed
    source.write_bytes(b"forged replacement")
    with pytest.raises(ValueError, match="recovery source does not match"):
        registry.publish(source, signature, recovery_source=source)
    assert os.fstat(recovery_files[0].fileno()).st_size == payload_size
    source.unlink()
    restored = tmp_path / "restored-large-result.md"
    with restored.open("wb") as destination:
        assert registry.copy_recovery_payload_to(source, destination)
    assert file_size_and_sha256(restored) == signature

    registry.close()
    registry.close()

    assert recovery_files[0].closed


def test_runtime_recovery_descriptor_count_is_bounded() -> None:
    script = textwrap.dedent(
        """
        import hashlib
        import resource
        import tempfile
        from pathlib import Path

        from crewplane.runtime.execution.publication_registry import (
            RuntimePublicationRegistry,
        )

        _, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        soft_limit = (
            64
            if hard_limit == resource.RLIM_INFINITY
            else min(64, hard_limit)
        )
        if soft_limit < 32:
            raise RuntimeError("Descriptor limit is too low for this regression test.")
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft_limit, hard_limit))
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source = root_path / "source.bin"
            source.write_bytes(b"x")
            signature = (1, hashlib.sha256(b"x").hexdigest())
            registry = RuntimePublicationRegistry()
            for index in range(128):
                registry.publish(
                    root_path / f"publication-{index}.bin",
                    signature,
                    recovery_source=source,
                )
            registry.close()
        print(index + 1)
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "128"

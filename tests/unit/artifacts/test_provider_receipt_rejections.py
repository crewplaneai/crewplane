from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from crewplane.artifacts.locks.manifest import LockManifestError, LockRunMetadata
from crewplane.artifacts.locks.provider_processes import (
    ensure_no_live_provider_processes,
)
from crewplane.artifacts.naming import build_provider_process_state_filename
from crewplane.core.provider_process_state import ProviderProcessState
from tests.helpers.resume_locks import FakeProcessInspector


@pytest.mark.parametrize("kind", ["incomplete-id", "incomplete-key", "escaping-key"])
def test_provider_recovery_rejects_incomplete_or_escaping_run_metadata(
    tmp_path: Path, kind: str
) -> None:
    metadata = LockRunMetadata(
        run_id=None if kind == "incomplete-id" else "run",
        run_key_name=None
        if kind == "incomplete-key"
        else "../escape"
        if kind == "escaping-key"
        else "flow--run",
        workflow_identity="flow",
        workflow_signature="signature",
    )

    with pytest.raises(LockManifestError, match="metadata"):
        ensure_no_live_provider_processes(
            tmp_path, metadata, FakeProcessInspector(123, "start")
        )

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "kind",
    [
        "wrong-filename",
        "directory-entry",
        "temporary-name",
        "temporary-no-token",
        "temporary-hardlink",
        "wrong-transition",
        "missing-group",
    ],
)
def test_provider_recovery_rejects_untrusted_receipts_and_partial_publications(
    tmp_path: Path, kind: str
) -> None:
    metadata, path, state = _receipt(tmp_path)
    inspector = FakeProcessInspector(123, "start")
    ensure_no_live_provider_processes(tmp_path, metadata, inspector)
    if kind == "wrong-filename":
        path.rename(path.with_name("wrong.json"))
    elif kind == "directory-entry":
        (path.parent / "unexpected").mkdir()
    elif kind == "temporary-name":
        (path.parent / "unexpected.tmp").write_bytes(b"unexpected")
    elif kind == "temporary-no-token":
        (path.parent / f".{path.name}..tmp").write_bytes(b"unexpected")
    elif kind == "temporary-hardlink":
        temporary = path.parent / f".{path.name}.token.tmp"
        temporary.write_text(state.model_dump_json())
        os.link(temporary, tmp_path / "external-alias")
    elif kind == "wrong-transition":
        (path.parent / f".{path.name}.token.tmp").write_text(state.model_dump_json())
    else:

        class UnverifiableGroup(FakeProcessInspector):
            def is_process_group_live(self, process_group_id: int) -> bool:
                raise RuntimeError(f"cannot inspect {process_group_id}")

        inspector = UnverifiableGroup(123, "start")

    with pytest.raises(
        LockManifestError, match="filename|safe file|does not match|process group"
    ):
        ensure_no_live_provider_processes(tmp_path, metadata, inspector)

    assert path.parent.exists()


@pytest.mark.parametrize(
    "operation", ["directory-stat", "listing", "receipt-stat", "receipt-read"]
)
@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_provider_recovery_preserves_files_on_inspection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    error_type: type[OSError],
) -> None:
    metadata, path, _state = _receipt(tmp_path)
    failure = error_type("receipt unavailable")
    method = {
        "directory-stat": "lstat",
        "listing": "iterdir",
        "receipt-stat": "lstat",
        "receipt-read": "read_text",
    }[operation]
    target = path.parent if operation in {"directory-stat", "listing"} else path
    original = getattr(Path, method)
    calls = 0

    def inspect(candidate: Path, *args: object, **kwargs: object) -> object:
        nonlocal calls
        if candidate == target:
            calls += 1
            # The containment walk is allowed to finish before receipt inspection.
            if method != "lstat" or calls > 1:
                raise failure
        return original(candidate, *args, **kwargs)

    before = path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(Path, method, inspect)
        with pytest.raises(
            PermissionError if error_type is PermissionError else LockManifestError
        ):
            ensure_no_live_provider_processes(
                tmp_path, metadata, FakeProcessInspector(123, "start")
            )

    assert path.read_bytes() == before


def _receipt(tmp_path: Path) -> tuple[LockRunMetadata, Path, ProviderProcessState]:
    metadata = LockRunMetadata("run", "flow--run", "flow", "signature")
    state = ProviderProcessState(
        run_state_schema_version=1,
        run_id="run",
        run_key_name="flow--run",
        node_id="draft",
        task_id="executor",
        provider="local",
        role="executor",
        round_num=1,
        attempt=1,
        pid=123,
        process_group_id=123,
        hostname="host",
        process_start_identity="start",
        status="started",
        started_at="2026-01-01T00:00:00",
    )
    directory = (
        tmp_path / "execution-stages" / "flow--run" / "manifests" / "provider-processes"
    )
    directory.mkdir(parents=True)
    filename = build_provider_process_state_filename(
        "draft", "executor", "local", "executor", None, 1, 1
    )
    path = directory / filename
    path.write_text(json.dumps(state.model_dump(mode="json")))
    return metadata, path, state

import asyncio
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from crewplane.runtime.execution.errors import NodeExecutionError
from crewplane.runtime.execution.review_loop.drift import (
    capture as review_loop_drift_capture,
)
from crewplane.runtime.execution.review_loop.drift import (
    comparison as review_loop_drift_comparison,
)
from crewplane.runtime.execution.review_loop.drift import (
    guard as review_loop_drift_guard,
)
from crewplane.runtime.execution.review_loop.drift import (
    recovery as review_loop_drift_recovery,
)
from crewplane.runtime.execution.review_loop.drift import (
    snapshots as review_loop_drift_snapshots,
)
from tests.integration.runtime.execution.review_loop_drift_support import (
    make_drift_request,
)


def test_node_local_unexpected_writes_are_warning_level(tmp_path: Path) -> None:
    _, output, node_dir = make_drift_request(tmp_path)
    unexpected = node_dir / "review-state" / "mutated-note.md"

    drift = review_loop_drift_comparison.detect_artifact_drift(
        before_snapshot={},
        after_snapshot={unexpected: (1, "hash")},
        allowed_paths=set(),
        output=output,
    )

    assert drift.warning_paths == (unexpected,)
    assert drift.fatal_paths == ()


def test_node_local_child_write_preserves_existing_directory_state(
    tmp_path: Path,
) -> None:
    request, _output, node_dir = make_drift_request(tmp_path)
    review_state = node_dir / "review-state"
    review_state.mkdir()
    prior_state = review_state / "prior.state.json"
    prior_state.write_text('{"round": 0}\n', encoding="utf-8")
    provider_note = review_state / "provider-note.md"

    class NodeLocalWritingInvoker:
        def log_presentation_for(self, config):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by protocol.
            return None

        async def invoke(  # type: ignore[no-untyped-def]
            self,
            config,  # noqa: ARG002 - Required by protocol.
            model,  # noqa: ARG002 - Required by protocol.
            prompt,  # noqa: ARG002 - Required by protocol.
            output_file,
            cwd,  # noqa: ARG002 - Required by protocol.
            log_file=None,  # noqa: ARG002 - Required by protocol.
            invocation_context=None,  # noqa: ARG002 - Required by protocol.
        ) -> None:
            provider_note.write_text("provider note\n", encoding="utf-8")
            output_file.write_text("provider output\n", encoding="utf-8")

    request.invoker = NodeLocalWritingInvoker()

    warning_count = asyncio.run(
        review_loop_drift_guard.run_provider_call_with_drift_guard(request)
    )

    assert warning_count == 1
    assert prior_state.read_text(encoding="utf-8") == '{"round": 0}\n'
    assert provider_note.read_text(encoding="utf-8") == "provider note\n"


def test_fatal_node_root_drift_is_restored(tmp_path: Path) -> None:
    request, _output, node_dir = make_drift_request(tmp_path)
    original_mode = stat.S_IMODE(node_dir.stat().st_mode)
    request.invocation_output_file = tmp_path / "provider-output.md"
    request.defer_output_publication = True

    class RootMetadataMutatingInvoker:
        def log_presentation_for(self, config):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by protocol.
            return None

        async def invoke(  # type: ignore[no-untyped-def]
            self,
            config,  # noqa: ARG002 - Required by protocol.
            model,  # noqa: ARG002 - Required by protocol.
            prompt,  # noqa: ARG002 - Required by protocol.
            output_file,
            cwd,  # noqa: ARG002 - Required by protocol.
            log_file=None,  # noqa: ARG002 - Required by protocol.
            invocation_context=None,  # noqa: ARG002 - Required by protocol.
        ) -> None:
            output_file.write_text("provider output\n", encoding="utf-8")
            node_dir.chmod(0o555)

    request.invoker = RootMetadataMutatingInvoker()

    try:
        with pytest.raises(NodeExecutionError, match="modified fatal artifacts"):
            asyncio.run(
                review_loop_drift_guard.run_provider_call_with_drift_guard(request)
            )
        assert stat.S_IMODE(node_dir.stat().st_mode) == original_mode
    finally:
        node_dir.chmod(original_mode)


def test_allowed_output_parent_replacement_is_fatal(tmp_path: Path) -> None:
    request, _output, node_dir = make_drift_request(tmp_path)
    audit_dir = node_dir / "review-audit-round-1"
    audit_dir.mkdir()
    request.output_file = audit_dir / "exec_executor_0_round1.md"
    request.allowed_paths = {request.output_file}
    displaced_dir = tmp_path / "displaced-audit-round-1"

    class OutputParentReplacingInvoker:
        def log_presentation_for(self, config):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by protocol.
            return None

        async def invoke(  # type: ignore[no-untyped-def]
            self,
            config,  # noqa: ARG002 - Required by protocol.
            model,  # noqa: ARG002 - Required by protocol.
            prompt,  # noqa: ARG002 - Required by protocol.
            output_file,
            cwd,  # noqa: ARG002 - Required by protocol.
            log_file=None,  # noqa: ARG002 - Required by protocol.
            invocation_context=None,  # noqa: ARG002 - Required by protocol.
        ) -> None:
            audit_dir.rename(displaced_dir)
            audit_dir.mkdir()
            output_file.write_text("provider output\n", encoding="utf-8")

    request.invoker = OutputParentReplacingInvoker()

    with pytest.raises(NodeExecutionError, match="modified fatal artifacts"):
        asyncio.run(review_loop_drift_guard.run_provider_call_with_drift_guard(request))


def test_fatal_node_directory_drift_restores_unchanged_descendants(
    tmp_path: Path,
) -> None:
    request, _output, node_dir = make_drift_request(tmp_path)
    review_state = node_dir / "review-state"
    review_state.mkdir()
    original_mode = stat.S_IMODE(review_state.stat().st_mode)
    prior_state = review_state / "prior.state.json"
    prior_state.write_text('{"round": 0}\n', encoding="utf-8")
    untouched_script = review_state / "untouched.sh"
    untouched_script.write_text("#!/bin/sh\n", encoding="utf-8")
    untouched_script.chmod(0o754)
    untouched_link = review_state / "untouched-link"
    untouched_link.symlink_to(untouched_script.name)

    class DirectoryMetadataMutatingInvoker:
        def log_presentation_for(self, config):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by protocol.
            return None

        async def invoke(  # type: ignore[no-untyped-def]
            self,
            config,  # noqa: ARG002 - Required by protocol.
            model,  # noqa: ARG002 - Required by protocol.
            prompt,  # noqa: ARG002 - Required by protocol.
            output_file,
            cwd,  # noqa: ARG002 - Required by protocol.
            log_file=None,  # noqa: ARG002 - Required by protocol.
            invocation_context=None,  # noqa: ARG002 - Required by protocol.
        ) -> None:
            review_state.chmod(original_mode ^ stat.S_IWGRP)
            output_file.write_text("provider output\n", encoding="utf-8")

    request.invoker = DirectoryMetadataMutatingInvoker()

    with pytest.raises(NodeExecutionError, match="modified fatal artifacts"):
        asyncio.run(review_loop_drift_guard.run_provider_call_with_drift_guard(request))

    assert stat.S_IMODE(review_state.stat().st_mode) == original_mode
    assert prior_state.read_text(encoding="utf-8") == '{"round": 0}\n'
    assert stat.S_IMODE(untouched_script.stat().st_mode) == 0o754
    assert untouched_link.is_symlink()
    assert untouched_link.readlink() == Path(untouched_script.name)


def test_directory_restore_restores_group_before_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _output, node_dir = make_drift_request(tmp_path)
    target = node_dir / "review-state"
    target.mkdir()
    monitoring_window = review_loop_drift_capture.capture_drift_monitoring_window(
        node_id=request.node.id,
        node_dir=node_dir,
        output=request.output,
        telemetry=None,
        runtime_publications=request.runtime_context.runtime_publications,
    )
    expected = monitoring_window.node_directory_snapshot[target]
    expected = replace(
        expected,
        group_id=expected.group_id + 1,
        mode=expected.mode ^ stat.S_IWGRP,
    )
    monitoring_window.node_directory_snapshot[target] = expected
    calls: list[tuple[str, int, int | None]] = []

    def record_chown(path: Path, user_id: int, group_id: int) -> None:
        assert path == target
        calls.append(("chown", user_id, group_id))

    def record_chmod(path: Path, mode: int) -> None:
        assert path == target
        calls.append(("chmod", mode, None))

    monkeypatch.setattr(os, "chown", record_chown)
    monkeypatch.setattr(Path, "chmod", record_chmod)

    review_loop_drift_recovery.restore_fatal_artifacts(
        request,
        monitoring_window,
        (target,),
    )

    assert calls == [
        ("chown", expected.user_id, expected.group_id),
        ("chmod", stat.S_IMODE(expected.mode), None),
    ]


def test_drift_snapshots_record_unsafe_entries_without_reading_targets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    symlink = root / "linked.txt"
    hardlink = root / "hardlinked.txt"
    try:
        symlink.symlink_to(outside)
        os.link(outside, hardlink)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"link creation is unavailable: {exc}")

    snapshot = review_loop_drift_snapshots.snapshot_files(root)

    assert symlink in snapshot
    assert hardlink in snapshot
    assert review_loop_drift_snapshots.snapshot_file_bytes(root) == {}


def test_drift_snapshots_reject_a_symlinked_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_root = tmp_path / "linked-root"
    try:
        linked_root.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        review_loop_drift_snapshots.snapshot_files(linked_root)


def test_drift_snapshot_ignores_a_file_that_disappears_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    transient = root / ".runtime-publication.tmp"
    transient.write_text("publishing", encoding="utf-8")
    original_signature = review_loop_drift_snapshots.file_snapshot_signature

    def remove_transient_before_read(path: Path) -> tuple[int, str]:
        if path == transient:
            path.unlink()
        return original_signature(path)

    monkeypatch.setattr(
        review_loop_drift_snapshots,
        "file_snapshot_signature",
        remove_transient_before_read,
    )

    assert review_loop_drift_snapshots.snapshot_files(root) == {}

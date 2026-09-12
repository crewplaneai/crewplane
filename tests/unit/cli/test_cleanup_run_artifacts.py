from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewplane.artifacts.manager import OutputManager
from crewplane.cli.workspace_cleanup.run_artifacts import load_workspace_manifest
from crewplane.observability.run_summary.workspace_readers import workspace_descriptor
from tests.helpers.resume import make_run_manifest


@pytest.mark.parametrize("workspace", [{}, {"source": {"run_base_commit": "abc"}}])
def test_written_run_manifest_is_shared_by_cleanup_and_summary(tmp_path, workspace):
    output = OutputManager("Workflow", base_dir=tmp_path)
    manifest = make_run_manifest(output.run_id, output.run_key_name).model_copy(
        update={"workspace": workspace}
    )
    output.write_run_manifest(manifest)
    preflight = output.stages_dir / "preflight" / "manifest.json"
    preflight.parent.mkdir()
    preflight.write_text(json.dumps({"workspace": {"fallback": True}}))

    loaded = load_workspace_manifest(
        output.stages_dir.parent.parent, output.run_key_name
    )

    assert loaded.error is None
    assert loaded.manifest == manifest
    assert workspace_descriptor(output.stages_dir) == workspace


@pytest.mark.parametrize(
    "current", [None, "{", "[]", "{}", '{"workspace":null}', '{"workspace":[]}']
)
def test_summary_falls_back_when_current_manifest_has_no_workspace_mapping(
    tmp_path, current
):
    path = tmp_path / "manifests" / "run.json"
    path.parent.mkdir()
    if current is not None:
        path.write_text(current)
    preflight = tmp_path / "preflight" / "manifest.json"
    preflight.parent.mkdir()
    preflight.write_text('{"workspace":{"fallback":true}}')

    assert workspace_descriptor(tmp_path) == {"fallback": True}


def test_manifest_symlink_is_unsafe_for_cleanup_and_skipped_by_summary(tmp_path):
    output = OutputManager("Workflow", base_dir=tmp_path)
    manifest = make_run_manifest(output.run_id, output.run_key_name)
    path = output.write_run_manifest(manifest)
    target = tmp_path / "outside.json"
    path.rename(target)
    path.symlink_to(target)
    loaded = load_workspace_manifest(
        output.stages_dir.parent.parent, output.run_key_name
    )

    assert loaded.manifest is None
    assert loaded.error == "run manifest is missing or unsafe"
    assert workspace_descriptor(output.stages_dir) is None
    assert path.is_symlink()


@pytest.mark.parametrize("run_key", ["", "../outside", "absolute", "run/../../outside"])
def test_cleanup_manifest_locator_rejects_unsafe_run_paths(tmp_path, run_key):
    state_dir = tmp_path / "state"
    stages = state_dir / "execution-stages"
    stages.mkdir(parents=True)
    outside = state_dir / "outside"
    if run_key == "absolute":
        run_key = outside.as_posix()
    if run_key:
        manifest_path = outside / "manifests" / "run.json"
    else:
        manifest_path = stages / "manifests" / "run.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(make_run_manifest("run", "run").model_dump_json())
    (stages / "run").mkdir()
    loaded = load_workspace_manifest(state_dir, run_key)
    assert loaded.manifest is None
    assert loaded.error == "run manifest is missing or unsafe"


def test_cleanup_manifest_locator_preserves_lookup_errors(tmp_path, monkeypatch):
    def fail_stat(path: Path, *args, **kwargs):
        del path, args, kwargs
        raise OSError("lookup failed")

    monkeypatch.setattr(Path, "lstat", fail_stat)
    loaded = load_workspace_manifest(tmp_path, "run")
    assert loaded.error == "run manifest is missing or unsafe"

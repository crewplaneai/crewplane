from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.release import build, state


def manifest_context(root: Path) -> state.ReleaseContext:
    return state.ReleaseContext(
        root,
        "crewplane",
        "crewplane",
        state.ReleaseVersion.from_project("1.2.3-alpha.4"),
    )


def reader_payload() -> dict:
    return {
        "schema_version": 1,
        "source_commit": "validated-source",
        "package": {
            "name": "crewplane",
            "project_version": "1.2.3-alpha.4",
            "python_version": "1.2.3a4",
            "npm_version": "1.2.3-alpha.4",
            "git_tag": "v1.2.3-alpha.4",
        },
        "artifacts": {
            "pypi_sdist": {
                "path": "dist/crewplane-1.2.3a4.tar.gz",
                "filename": "crewplane-1.2.3a4.tar.gz",
                "size": 12,
                "sha256": "sdist-sha256",
                "sha1": "sdist-sha1",
                "integrity": "sha512-sdist",
            }
        },
    }


def write_reader_payload(root: Path, payload: object) -> None:
    path = root / state.MANIFEST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_manifest_writer_preserves_golden_bytes_and_original_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = manifest_context(tmp_path)
    artifacts = {
        "plugin:crewplane-ollama:wheel": state.ArtifactIdentity(
            "identity-key-is-not-serialized",
            "dist/café.whl",
            "café.whl",
            7,
            "plugin-sha256",
        ),
        "npm_tarball": state.ArtifactIdentity(
            "npm_tarball",
            ".release/npm/crewplane-1.2.3-alpha.4.tgz",
            "crewplane-1.2.3-alpha.4.tgz",
            13,
            "npm-sha256",
            "npm-sha1",
            "sha512-npm",
        ),
    }
    serialized = []
    writes = []

    def serialize(manifest: state.ReleaseManifest) -> dict:
        serialized.append(manifest)
        return state.serialize_manifest(manifest)

    def write_json(path: Path, payload: dict) -> None:
        writes.append(path)
        state.write_json(path, payload)

    monkeypatch.setattr(build, "serialize_manifest", serialize)
    monkeypatch.setattr(build, "write_json", write_json)

    manifest = build.write_release_manifest(context, artifacts)

    expected = (
        b"{\n"
        b'  "schema_version": 1,\n'
        b'  "source_commit": "",\n'
        b'  "package": {\n'
        b'    "name": "crewplane",\n'
        b'    "project_version": "1.2.3-alpha.4",\n'
        b'    "python_version": "1.2.3a4",\n'
        b'    "npm_version": "1.2.3-alpha.4",\n'
        b'    "git_tag": "v1.2.3-alpha.4"\n'
        b"  },\n"
        b'  "artifacts": {\n'
        b'    "plugin:crewplane-ollama:wheel": {\n'
        b'      "path": "dist/caf\\u00e9.whl",\n'
        b'      "filename": "caf\\u00e9.whl",\n'
        b'      "size": 7,\n'
        b'      "sha256": "plugin-sha256",\n'
        b'      "sha1": "",\n'
        b'      "integrity": ""\n'
        b"    },\n"
        b'    "npm_tarball": {\n'
        b'      "path": ".release/npm/crewplane-1.2.3-alpha.4.tgz",\n'
        b'      "filename": "crewplane-1.2.3-alpha.4.tgz",\n'
        b'      "size": 13,\n'
        b'      "sha256": "npm-sha256",\n'
        b'      "sha1": "npm-sha1",\n'
        b'      "integrity": "sha512-npm"\n'
        b"    }\n"
        b"  }\n"
        b"}\n"
    )
    active = tmp_path / ".release/release-manifest.json"
    archive = tmp_path / ".release-manifests/release-manifest.1.2.3-alpha.4.json"
    assert writes == [active, archive]
    assert active.read_bytes() == archive.read_bytes() == expected
    assert len(serialized) == 1
    assert serialized[0] is manifest
    assert manifest.artifacts is artifacts


def test_manifest_round_trip_preserves_plugins_every_field_and_mapping_order(
    tmp_path: Path,
) -> None:
    artifacts = {
        key: state.ArtifactIdentity(key, path, filename, size, sha256, sha1, integrity)
        for key, path, filename, size, sha256, sha1, integrity in (
            (
                "pypi_sdist",
                "dist/crewplane-1.2.3a4.tar.gz",
                "crewplane-1.2.3a4.tar.gz",
                11,
                "sdist-sha256",
                "sdist-sha1",
                "sha512-sdist",
            ),
            (
                "pypi_wheel",
                "dist/crewplane-1.2.3a4-py3-none-any.whl",
                "crewplane-1.2.3a4-py3-none-any.whl",
                22,
                "wheel-sha256",
                "wheel-sha1",
                "sha512-wheel",
            ),
            (
                "npm_tarball",
                ".release/npm/crewplane-1.2.3-alpha.4.tgz",
                "crewplane-1.2.3-alpha.4.tgz",
                33,
                "npm-sha256",
                "npm-sha1",
                "sha512-npm",
            ),
            (
                "plugin:crewplane-ollama:sdist",
                "dist/crewplane_ollama-0.1.0.tar.gz",
                "crewplane_ollama-0.1.0.tar.gz",
                44,
                "plugin-sdist-sha256",
                "plugin-sdist-sha1",
                "sha512-plugin-sdist",
            ),
            (
                "plugin:crewplane-ollama:wheel",
                "dist/crewplane_ollama-0.1.0-py3-none-any.whl",
                "crewplane_ollama-0.1.0-py3-none-any.whl",
                55,
                "plugin-wheel-sha256",
                "plugin-wheel-sha1",
                "sha512-plugin-wheel",
            ),
        )
    }

    manifest = build.write_release_manifest(
        manifest_context(tmp_path), artifacts, "validated-source"
    )
    loaded = state.read_manifest(tmp_path)

    assert loaded == manifest
    assert list(loaded.artifacts) == [
        "pypi_sdist",
        "pypi_wheel",
        "npm_tarball",
        "plugin:crewplane-ollama:sdist",
        "plugin:crewplane-ollama:wheel",
    ]
    assert loaded.source_commit == "validated-source"
    assert manifest.artifacts is artifacts


def test_manifest_writer_replaces_active_but_retains_versioned_archive(tmp_path: Path):
    context = manifest_context(tmp_path)
    build.write_release_manifest(context, {}, "first-source")
    first_archive = tmp_path / ".release-manifests/release-manifest.1.2.3-alpha.4.json"
    original_bytes = first_archive.read_bytes()
    next_context = replace(context, version=state.ReleaseVersion.from_project("1.2.3"))

    manifest = build.write_release_manifest(next_context, {}, "second-source")

    active = tmp_path / ".release/release-manifest.json"
    next_archive = tmp_path / ".release-manifests/release-manifest.1.2.3.json"
    assert first_archive.read_bytes() == original_bytes
    assert active.read_bytes() == next_archive.read_bytes()
    assert state.read_manifest(tmp_path) == manifest
    assert manifest.source_commit == "second-source"
    assert manifest.artifacts == {}


def test_manifest_reader_defaults_only_missing_optional_fields(tmp_path: Path) -> None:
    payload = reader_payload()
    del payload["source_commit"]
    artifact = payload["artifacts"]["pypi_sdist"]
    del artifact["sha1"]
    del artifact["integrity"]
    write_reader_payload(tmp_path, payload)

    manifest = state.read_manifest(tmp_path)

    assert manifest.source_commit == ""
    assert manifest.artifact("pypi_sdist").sha1 == ""
    assert manifest.artifact("pypi_sdist").integrity == ""


def test_manifest_reader_keeps_explicit_null_string_coercions(tmp_path: Path) -> None:
    payload = reader_payload()
    payload["package"] = {key: None for key in payload["package"]}
    payload["artifacts"]["pypi_sdist"].update(
        path=None, filename=None, sha256=None, sha1=None, integrity=None
    )
    write_reader_payload(tmp_path, payload)

    manifest = state.read_manifest(tmp_path)

    assert (
        manifest.package_name,
        manifest.project_version,
        manifest.python_version,
        manifest.npm_version,
        manifest.git_tag,
    ) == ("None",) * 5
    artifact = manifest.artifact("pypi_sdist")
    assert (
        artifact.path,
        artifact.filename,
        artifact.sha256,
        artifact.sha1,
        artifact.integrity,
    ) == ("None",) * 5


@pytest.mark.parametrize("source_commit", [None, 123, False, [], {}])
def test_manifest_reader_rejects_non_string_source_commit(
    tmp_path: Path, source_commit
):
    payload = reader_payload()
    payload["source_commit"] = source_commit
    write_reader_payload(tmp_path, payload)

    with pytest.raises(state.ReleaseError, match="source commit must be a string"):
        state.read_manifest(tmp_path)


@pytest.mark.parametrize(("size", "expected"), [("007", 7), (2.9, 2), (True, 1)])
def test_manifest_reader_keeps_numeric_and_string_coercions(
    tmp_path: Path, size, expected
):
    payload = reader_payload()
    payload["package"] = {key: 123 for key in payload["package"]}
    payload["artifacts"]["pypi_sdist"].update(
        path=123, filename=456, size=size, sha256=789, sha1=False, integrity=1.5
    )
    write_reader_payload(tmp_path, payload)

    manifest = state.read_manifest(tmp_path)

    assert (
        manifest.package_name,
        manifest.project_version,
        manifest.python_version,
        manifest.npm_version,
        manifest.git_tag,
    ) == ("123",) * 5
    assert manifest.artifact("pypi_sdist") == state.ArtifactIdentity(
        "pypi_sdist", "123", "456", expected, "789", "False", "1.5"
    )


@pytest.mark.parametrize("schema", [None, 0, 2, "future", {"unrecognized": True}])
def test_manifest_reader_does_not_enforce_schema_marker(tmp_path: Path, schema):
    payload = reader_payload()
    payload["schema_version"] = schema
    write_reader_payload(tmp_path, payload)
    with_schema = state.read_manifest(tmp_path)
    del payload["schema_version"]
    write_reader_payload(tmp_path, payload)

    assert state.read_manifest(tmp_path) == with_schema


@pytest.mark.parametrize("field", ["package", "artifacts"])
@pytest.mark.parametrize("invalid", [None, [], "malformed", 123])
def test_manifest_reader_rejects_malformed_top_level_shapes(tmp_path, field, invalid):
    payload = reader_payload()
    payload[field] = invalid
    write_reader_payload(tmp_path, payload)

    with pytest.raises(state.ReleaseError, match="invalid shape"):
        state.read_manifest(tmp_path)


@pytest.mark.parametrize("invalid", [None, [], "malformed", 123])
def test_manifest_reader_rejects_malformed_artifact_shapes(tmp_path, invalid):
    payload = reader_payload()
    payload["artifacts"]["pypi_sdist"] = invalid
    write_reader_payload(tmp_path, payload)

    with pytest.raises(state.ReleaseError, match="artifact 'pypi_sdist' is malformed"):
        state.read_manifest(tmp_path)


@pytest.mark.parametrize(
    "field", ["name", "project_version", "python_version", "npm_version", "git_tag"]
)
def test_manifest_reader_preserves_missing_package_field_errors(tmp_path, field):
    payload = reader_payload()
    del payload["package"][field]
    write_reader_payload(tmp_path, payload)

    with pytest.raises(KeyError) as failure:
        state.read_manifest(tmp_path)

    assert failure.value.args == (field,)


@pytest.mark.parametrize("field", ["path", "filename", "size", "sha256"])
def test_manifest_reader_preserves_missing_artifact_field_errors(tmp_path, field):
    payload = reader_payload()
    del payload["artifacts"]["pypi_sdist"][field]
    write_reader_payload(tmp_path, payload)

    with pytest.raises(KeyError) as failure:
        state.read_manifest(tmp_path)

    assert failure.value.args == (field,)


@pytest.mark.parametrize(
    ("size", "error"), [(None, TypeError), ([], TypeError), ("invalid", ValueError)]
)
def test_manifest_reader_preserves_invalid_size_errors(tmp_path, size, error):
    payload = reader_payload()
    payload["artifacts"]["pypi_sdist"]["size"] = size
    write_reader_payload(tmp_path, payload)

    with pytest.raises(error):
        state.read_manifest(tmp_path)


@pytest.mark.parametrize(
    ("content", "message"),
    [("[1, 2]", "expected a JSON object"), ("{invalid", "invalid JSON")],
)
def test_manifest_reader_preserves_json_errors(tmp_path, content, message):
    path = tmp_path / state.MANIFEST_PATH
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")

    with pytest.raises(state.ReleaseError, match=message):
        state.read_manifest(tmp_path)


def test_manifest_writer_keeps_read_back_failure_after_both_writes(
    tmp_path, monkeypatch
):
    context = manifest_context(tmp_path)
    writes = []

    def corrupt_active(path: Path, payload: dict) -> None:
        writes.append(path)
        state.write_json(
            path, {} if path == tmp_path / state.MANIFEST_PATH else payload
        )

    monkeypatch.setattr(build, "write_json", corrupt_active)

    with pytest.raises(
        state.ReleaseError, match="release manifest read-back validation failed"
    ):
        build.write_release_manifest(context, {})

    active = tmp_path / ".release/release-manifest.json"
    archive = tmp_path / ".release-manifests/release-manifest.1.2.3-alpha.4.json"
    assert writes == [active, archive]
    assert json.loads(active.read_text(encoding="utf-8")) == {}
    assert (
        json.loads(archive.read_text(encoding="utf-8"))["package"]["name"]
        == "crewplane"
    )

from __future__ import annotations

import base64
import hashlib
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.release import build, publish, state
from scripts.release.artifact_layout import release_artifact_layout
from scripts.release.plugins import release_plugins
from tests.helpers.platforms import symlink_or_skip
from tests.unit.packaging.release_tool_support import (
    FakeRunner,
    constant,
    matching_npm,
    matching_pypi,
    no_op,
    release_state_fixture,
)

ARTIFACTS = (
    (
        "pypi_sdist",
        "dist/crewplane-1.2.3.tar.gz",
        b"a",
        "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb",
    ),
    (
        "pypi_wheel",
        "dist/crewplane-1.2.3-py3-none-any.whl",
        b"b",
        "3e23e8160039594a33894f6564e1b1348bbd7a0088d42c4acb73eeaed59c009d",
    ),
    (
        "npm_tarball",
        ".release/npm/crewplane-1.2.3.tgz",
        b"c",
        "2e7d2c03a9507ae265ecf5b5356885a53393a2029d241394997265a1a25aefc6",
    ),
    (
        "plugin:crewplane-ollama:sdist",
        "dist/crewplane_ollama-0.1.0.tar.gz",
        b"abc",
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    ),
    (
        "plugin:crewplane-ollama:wheel",
        "dist/crewplane_ollama-0.1.0-py3-none-any.whl",
        b"",
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ),
)


@pytest.fixture
def release_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    context, _manifest, formula, git = release_state_fixture(tmp_path, "1.2.3")
    project = tmp_path / "pyproject.toml"
    project.write_text(
        project.read_text().replace(
            'dependencies = ["typer>=0.12.0"]',
            'dependencies = ["typer>=0.12.0", "crewplane-ollama>=0.1.0,<0.2"]',
        )
        + '\n[tool.crewplane.release]\nplugins = ["plugins/crewplane-ollama"]\n'
    )
    source = tmp_path / "plugins/crewplane-ollama"
    source.mkdir(parents=True)
    (source / "pyproject.toml").write_text(
        '[project]\nname = "crewplane-ollama"\nversion = "0.1.0"\n'
    )
    artifacts = {}
    for key, relative, content, sha256 in ARTIFACTS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        artifacts[key] = state.artifact_identity(path, tmp_path, key)
        assert artifacts[key].sha256 == sha256
    manifest = build.write_release_manifest(context, artifacts, git.head_commit)
    (plugin,) = release_plugins(context)
    registries = {
        "crewplane": matching_pypi(context, manifest),
        "crewplane-ollama": matching_pypi(plugin.context, plugin.manifest(manifest)),
    }

    def query_pypi(package: state.ReleaseContext) -> state.PypiRelease:
        return registries[package.package_name]

    monkeypatch.setattr(publish, "query_pypi_release", query_pypi)
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(matching_npm(context, manifest, latest="1.2.3")),
    )
    monkeypatch.setattr(
        publish,
        "read_formula_state",
        constant(replace(formula, sha256=ARTIFACTS[0][3])),
    )
    monkeypatch.setattr(publish, "inspect_release_tag_state", constant(git))
    monkeypatch.setattr(publish, "verified_release_notes_start_tag", constant("v1.0.0"))
    return context, manifest, registries


def test_plugin_bundle_has_literal_layout_identities_and_verified_plan(release_bundle):
    context, manifest, _registries = release_bundle

    assert list(release_artifact_layout(context).items()) == [
        (key, Path(relative)) for key, relative, _content, _sha256 in ARTIFACTS
    ]
    assert list(manifest.artifacts) == [row[0] for row in ARTIFACTS]
    for key, relative, content, sha256 in ARTIFACTS:
        artifact = manifest.artifact(key)
        assert artifact.key == key
        assert Path(artifact.path) == Path(relative)
        assert artifact.filename == relative.rsplit("/", 1)[1]
        assert artifact.size == len(content)
        assert artifact.sha256 == sha256
        assert artifact.sha1 == hashlib.sha1(content).hexdigest()
        assert artifact.integrity == "sha512-" + base64.b64encode(
            hashlib.sha512(content).digest()
        ).decode("ascii")

    assert publish.github_release_bundle_issues(context, manifest) == []
    assert publish.verified_github_release_plan(context.root, FakeRunner()) == (
        publish.GitHubReleasePlan(False, True, "v1.0.0")
    )


def test_layout_retains_declared_plugin_order_instead_of_sorting(release_bundle):
    context, _manifest, _registries = release_bundle
    project = context.root / "pyproject.toml"
    project.write_text(
        project.read_text()
        .replace(
            '"crewplane-ollama>=0.1.0,<0.2"',
            '"crewplane-ollama>=0.1.0,<0.2", "crewplane-alpha==0.2.0"',
        )
        .replace(
            'plugins = ["plugins/crewplane-ollama"]',
            'plugins = ["plugins/crewplane-ollama", "plugins/crewplane-alpha"]',
        )
    )
    source = context.root / "plugins/crewplane-alpha"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname = "crewplane-alpha"\nversion = "0.2.0"\n'
    )

    assert list(release_artifact_layout(context).items()) == [
        ("pypi_sdist", Path("dist/crewplane-1.2.3.tar.gz")),
        ("pypi_wheel", Path("dist/crewplane-1.2.3-py3-none-any.whl")),
        ("npm_tarball", Path(".release/npm/crewplane-1.2.3.tgz")),
        (
            "plugin:crewplane-ollama:sdist",
            Path("dist/crewplane_ollama-0.1.0.tar.gz"),
        ),
        (
            "plugin:crewplane-ollama:wheel",
            Path("dist/crewplane_ollama-0.1.0-py3-none-any.whl"),
        ),
        (
            "plugin:crewplane-alpha:sdist",
            Path("dist/crewplane_alpha-0.2.0.tar.gz"),
        ),
        (
            "plugin:crewplane-alpha:wheel",
            Path("dist/crewplane_alpha-0.2.0-py3-none-any.whl"),
        ),
    ]


@pytest.mark.parametrize("change", ["missing-npm", "missing-plugin", "extra"])
def test_plugin_bundle_plan_requires_exact_manifest_keys(release_bundle, change):
    context, manifest, _registries = release_bundle
    if change == "extra":
        manifest.artifacts["undeclared"] = manifest.artifact("pypi_wheel")
    else:
        key = "npm_tarball" if change == "missing-npm" else ARTIFACTS[4][0]
        manifest.artifacts.pop(key)
    build.write_release_manifest(context, manifest.artifacts, manifest.source_commit)

    with pytest.raises(state.ReleaseError, match="must contain exactly"):
        publish.verified_github_release_plan(context.root, FakeRunner())


@pytest.mark.parametrize("key", [row[0] for row in ARTIFACTS])
def test_plugin_bundle_plan_rejects_noncanonical_paths(release_bundle, key):
    context, manifest, _registries = release_bundle
    artifact = manifest.artifact(key)
    alternate = context.root / "alternate" / artifact.filename
    alternate.parent.mkdir()
    alternate.write_bytes((context.root / artifact.path).read_bytes())
    manifest.artifacts[key] = replace(
        artifact, path=str(alternate.relative_to(context.root))
    )
    build.write_release_manifest(context, manifest.artifacts, manifest.source_commit)

    with pytest.raises(state.ReleaseError, match="unexpected (path|identity)"):
        publish.verified_github_release_plan(context.root, FakeRunner())


@pytest.mark.parametrize("relative", ["dist", ".release/npm"])
@pytest.mark.parametrize(
    "change", ["extra-file", "extra-directory", "missing", "file", "symlink"]
)
def test_plugin_bundle_plan_rejects_invalid_directories(
    release_bundle, relative, change
):
    context, _manifest, _registries = release_bundle
    directory = context.root / relative
    if change == "extra-file":
        (directory / "undeclared.txt").write_text("extra")
    elif change == "extra-directory":
        (directory / "undeclared").mkdir()
    elif change == "symlink":
        target = context.root / "relocated"
        directory.rename(target)
        symlink_or_skip(directory, target, target_is_directory=True)
    else:
        shutil.rmtree(directory)
        if change == "file":
            directory.write_text("not a directory")

    with pytest.raises(state.ReleaseError, match="release bundle directory"):
        publish.verified_github_release_plan(context.root, FakeRunner())


@pytest.mark.parametrize("key", [row[0] for row in ARTIFACTS])
@pytest.mark.parametrize("change", ["directory", "symlink", "missing", "changed"])
def test_plugin_bundle_plan_rejects_invalid_artifacts(release_bundle, key, change):
    context, manifest, _registries = release_bundle
    path = context.root / manifest.artifact(key).path
    content = path.read_bytes()
    path.unlink()
    if change == "directory":
        path.mkdir()
    elif change == "symlink":
        target = context.root / "relocated-artifact"
        target.write_bytes(content)
        symlink_or_skip(path, target)
    elif change == "changed":
        path.write_bytes(b"changed artifact")

    with pytest.raises(
        state.ReleaseError,
        match="(regular file|unexpected contents|sha256 does not match)",
    ):
        publish.verified_github_release_plan(context.root, FakeRunner())


@pytest.mark.parametrize("path_kind", ["absolute", "escape"])
def test_plugin_bundle_retains_independent_containment_checks(
    release_bundle, path_kind
):
    context, manifest, _registries = release_bundle
    artifact = manifest.artifact("npm_tarball")
    path = (
        str(context.root / artifact.path)
        if path_kind == "absolute"
        else "../outside.tgz"
    )
    manifest.artifacts["npm_tarball"] = replace(artifact, path=path)
    build.write_release_manifest(context, manifest.artifacts, manifest.source_commit)

    with pytest.raises(state.ReleaseError) as failure:
        publish.verified_github_release_plan(context.root, FakeRunner())

    assert "npm_tarball has an unexpected path" in str(failure.value)
    expected = (
        "uses an absolute path"
        if path_kind == "absolute"
        else "escapes the repository root"
    )
    assert expected in str(failure.value)


@pytest.mark.parametrize("field", ["sha1", "integrity"])
def test_plugin_bundle_retains_optional_identity_hash_checks(release_bundle, field):
    context, manifest, _registries = release_bundle
    manifest.artifacts["npm_tarball"] = replace(
        manifest.artifact("npm_tarball"), **{field: "incorrect"}
    )
    build.write_release_manifest(context, manifest.artifacts, manifest.source_commit)

    with pytest.raises(state.ReleaseError, match=f"npm_tarball {field} does not match"):
        publish.verified_github_release_plan(context.root, FakeRunner())


@pytest.mark.parametrize("change", ["source", "plugin-registry"])
def test_plugin_bundle_retains_source_and_registry_checks(release_bundle, change):
    context, manifest, registries = release_bundle
    if change == "source":
        build.write_release_manifest(context, manifest.artifacts, "different-source")
    else:
        registries["crewplane-ollama"] = state.PypiRelease(False, "", {})

    with pytest.raises(
        state.ReleaseError, match="(source commit|PyPI version is missing)"
    ):
        publish.verified_github_release_plan(context.root, FakeRunner())


def test_build_collects_literal_identities_after_python_only_twine(
    release_bundle, monkeypatch: pytest.MonkeyPatch
):
    context, manifest, _registries = release_bundle
    calls = []
    runner = FakeRunner()
    twine_check = build.run_twine_check

    def build_python(_context, _runner):
        del _context, _runner
        calls.append("build")

    def check_python(context_arg, runner_arg):
        calls.append("twine")
        twine_check(context_arg, runner_arg)

    def build_npm(_context, _runner):
        del _context, _runner
        calls.append("npm")
        return context.root / ".release/npm/crewplane-1.2.3.tgz"

    def identity(path, root, key):
        calls.append(key)
        return state.artifact_identity(path, root, key)

    monkeypatch.setattr(build, "build_python_artifacts", build_python)
    monkeypatch.setattr(build, "run_twine_check", check_python)
    monkeypatch.setattr(build, "build_npm_artifact", build_npm)
    monkeypatch.setattr(build, "artifact_identity", identity)

    artifacts = build.build_release_artifacts(context, runner)

    assert calls == ["build", "twine", "npm", *(row[0] for row in ARTIFACTS)]
    assert list(artifacts) == [row[0] for row in ARTIFACTS]
    assert artifacts == manifest.artifacts
    assert runner.commands == [
        (
            sys.executable,
            "-m",
            "twine",
            "check",
            str(context.root / "dist/crewplane-1.2.3.tar.gz"),
            str(context.root / "dist/crewplane-1.2.3-py3-none-any.whl"),
            str(context.root / "dist/crewplane_ollama-0.1.0.tar.gz"),
            str(context.root / "dist/crewplane_ollama-0.1.0-py3-none-any.whl"),
        )
    ]


def test_build_hashes_the_actual_returned_npm_path(release_bundle, monkeypatch):
    context, _manifest, _registries = release_bundle
    npm_path = context.root / ".release/returned-by-npm.tgz"
    npm_path.write_bytes(b"actual npm output")
    (context.root / ".release/npm/crewplane-1.2.3.tgz").unlink()
    monkeypatch.setattr(build, "build_python_artifacts", no_op)
    monkeypatch.setattr(build, "run_twine_check", no_op)
    monkeypatch.setattr(build, "build_npm_artifact", constant(npm_path))

    artifact = build.build_release_artifacts(context, FakeRunner())["npm_tarball"]

    assert Path(artifact.path) == Path(".release/returned-by-npm.tgz")
    assert artifact.filename == "returned-by-npm.tgz"
    assert artifact.sha256 == hashlib.sha256(b"actual npm output").hexdigest()


@pytest.mark.parametrize(
    "row", [ARTIFACTS[0], ARTIFACTS[1], ARTIFACTS[3], ARTIFACTS[4]]
)
def test_build_preserves_core_and_plugin_missing_file_failures(
    release_bundle, monkeypatch, row
):
    context, _manifest, _registries = release_bundle
    key, relative, _content, _sha256 = row
    (context.root / relative).unlink()
    hashed = []

    def identity(path, root, artifact_key):
        hashed.append(artifact_key)
        return state.artifact_identity(path, root, artifact_key)

    monkeypatch.setattr(build, "build_python_artifacts", no_op)
    monkeypatch.setattr(build, "run_twine_check", no_op)
    monkeypatch.setattr(
        build,
        "build_npm_artifact",
        constant(context.root / ".release/npm/crewplane-1.2.3.tgz"),
    )
    monkeypatch.setattr(build, "artifact_identity", identity)

    if key.startswith("pypi_"):
        with pytest.raises(
            state.ReleaseError, match="expected release artifact is missing"
        ):
            build.build_release_artifacts(context, FakeRunner())
        assert hashed == []
    else:
        with pytest.raises(FileNotFoundError) as failure:
            build.build_release_artifacts(context, FakeRunner())
        assert Path(failure.value.filename) == context.root / relative
        missing_index = [row[0] for row in ARTIFACTS].index(key)
        assert hashed == [row[0] for row in ARTIFACTS[: missing_index + 1]]

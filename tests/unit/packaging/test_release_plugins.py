import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.release import build, publish, state
from scripts.release.plugins import release_plugins
from tests.unit.packaging.release_tool_support import (
    FakeRunner,
    constant,
    matching_pypi,
    no_op,
    release_state_fixture,
)


@pytest.fixture
def plugin_release(tmp_path):
    context, manifest, _formula, git = release_state_fixture(tmp_path)
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
    (plugin,) = release_plugins(context)
    artifacts = {}
    paths = {key: Path(artifact.path) for key, artifact in manifest.artifacts.items()}
    paths.update(
        {
            plugin.artifact_key("sdist"): Path("dist") / plugin.context.sdist_filename,
            plugin.artifact_key("wheel"): Path("dist") / plugin.context.wheel_filename,
        }
    )
    for key, path in paths.items():
        full_path = tmp_path / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(path.name.encode())
        artifacts[key] = state.artifact_identity(full_path, tmp_path, key)
    return context, plugin, replace(manifest, artifacts=artifacts), git


def test_build_includes_default_plugin_distributions(plugin_release):
    context, plugin, _manifest, _git = plugin_release
    runner = FakeRunner()

    build.build_python_artifacts(context, runner)

    assert len(runner.commands) == 2
    assert runner.commands[1][-1] == str(plugin.source)
    assert runner.commands[1][-2] == str(context.root / "dist")
    assert plugin.context.wheel_filename == "crewplane_ollama-0.1.0-py3-none-any.whl"
    assert plugin.context.sdist_filename == "crewplane_ollama-0.1.0.tar.gz"


def test_release_manifest_captures_default_plugin_artifacts(
    plugin_release, monkeypatch
):
    context, plugin, manifest, _git = plugin_release
    monkeypatch.setattr(build, "build_python_artifacts", no_op)
    monkeypatch.setattr(build, "run_twine_check", no_op)
    monkeypatch.setattr(
        build,
        "build_npm_artifact",
        constant(context.root / manifest.artifact("npm_tarball").path),
    )

    artifacts = build.build_release_artifacts(context, FakeRunner())

    assert artifacts == manifest.artifacts
    assert artifacts[plugin.artifact_key("wheel")].sha256


def test_release_binding_rejects_changed_plugin_artifacts(plugin_release, monkeypatch):
    context, plugin, manifest, git = plugin_release
    monkeypatch.setattr(build, "read_clean_source_commit", constant(git.head_commit))
    artifact = manifest.artifact(plugin.artifact_key("wheel"))
    (context.root / artifact.path).write_bytes(b"changed after validation")

    with pytest.raises(state.ReleaseError, match="validated release artifacts changed"):
        build.bind_release_manifest(context, manifest, git.head_commit, FakeRunner())


def test_wheelhouse_resolves_default_plugin_from_built_wheel(
    plugin_release, monkeypatch
):
    context, plugin, _manifest, _git = plugin_release
    runner = FakeRunner()
    monkeypatch.setattr(build, "command_exists", constant(True))

    build.build_wheelhouse(context, runner)

    export, download = runner.commands
    index = export.index("--no-emit-package")
    assert export[index + 1] == plugin.context.package_name
    assert download[-2:] == (
        str(context.root / "dist" / context.wheel_filename),
        str(context.root / "dist" / plugin.context.wheel_filename),
    )


@pytest.mark.parametrize(
    "initial", ["missing", "complete", "sdist-only", "corrupt", "yanked"]
)
def test_plugin_is_verified_or_published_before_core(
    plugin_release, monkeypatch, initial
):
    context, plugin, manifest, git = plugin_release
    runner = FakeRunner()
    upload_run = Mock(wraps=runner.run)
    monkeypatch.setattr(runner, "run", upload_run)
    monkeypatch.setenv("PYPI_REPOSITORY", "release-test")
    monkeypatch.setenv(
        "TWINE_UPLOAD_ARGS", '--non-interactive --comment "Release validation"'
    )
    monkeypatch.setenv("PYPI_TOKEN", "pypi-test-token")
    monkeypatch.delenv("TWINE_PASSWORD", raising=False)
    plugin_manifest = plugin.manifest(manifest)

    def registry(package):
        selected = (
            plugin_manifest
            if package.package_name == plugin.context.package_name
            else manifest
        )
        uploaded = any(
            str(context.root / selected.artifact("pypi_wheel").path) in command
            for command in runner.commands
        )
        if uploaded:
            return matching_pypi(package, selected)
        if package.package_name == context.package_name or initial == "missing":
            return state.PypiRelease(False, "", {})
        remote = matching_pypi(package, selected)
        if initial == "complete":
            return remote
        if initial == "yanked":
            return replace(
                remote,
                files={
                    name: replace(file, yanked=True)
                    for name, file in remote.files.items()
                },
            )
        sdist = selected.artifact("pypi_sdist")
        file = remote.files[sdist.filename]
        if initial == "corrupt":
            file = replace(file, sha256="0" * 64)
        return replace(remote, files={sdist.filename: file})

    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "require_publish_git_state", constant(git))
    monkeypatch.setattr(publish, "require_pypi_auth", no_op)
    monkeypatch.setattr(publish.smoke, "post_publish_pypi_check", no_op)
    monkeypatch.setattr(publish, "query_pypi_release", registry)
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(state.NpmRelease(False, "", "", "", "", "", "", "", "")),
    )
    if initial in {"corrupt", "yanked"}:
        with pytest.raises(state.ReleaseError, match="Plugin publication is blocked"):
            publish.publish_pypi(context.root, runner, execute=True)
        assert not runner.commands
        if initial == "yanked":
            assert publish.plugin_publication_issues(context, manifest) == [
                f"PyPI artifact is yanked: {plugin.context.sdist_filename}",
                f"PyPI artifact is yanked: {plugin.context.wheel_filename}",
            ]
        return

    assert publish.publish_pypi(context.root, runner, execute=True) == 0
    assert context.wheel_filename in runner.commands[-1][-1]
    assert len(runner.commands) == (1 if initial == "complete" else 2)
    for call in upload_run.call_args_list:
        assert call.args[0][:9] == [
            sys.executable,
            "-m",
            "twine",
            "upload",
            "--repository",
            "release-test",
            "--non-interactive",
            "--comment",
            "Release validation",
        ]
        assert call.kwargs["cwd"] == context.root
        assert call.kwargs["env"] == {
            "TWINE_USERNAME": "__token__",
            "TWINE_PASSWORD": "pypi-test-token",
        }
        assert call.kwargs["capture_output"] is False
    if initial != "complete":
        assert plugin.context.wheel_filename in runner.commands[0][-1]
        sdist = str(context.root / "dist" / plugin.context.sdist_filename)
        assert (sdist in runner.commands[0]) == (initial == "missing")


@pytest.mark.parametrize("field", ["filename", "path", "missing"])
def test_plugin_manifest_rejects_incorrect_artifact_identity(plugin_release, field):
    context, plugin, manifest, _git = plugin_release
    key = plugin.artifact_key("wheel")
    if field == "missing":
        manifest.artifacts.pop(key)
    else:
        manifest.artifacts[key] = replace(
            manifest.artifacts[key], **{field: "wrong.whl"}
        )

    issues = state.manifest_context_issues(context, manifest)

    assert issues
    assert "artifact" in issues[0]


def test_declared_plugin_must_be_a_required_compatible_dependency(plugin_release):
    context, plugin, _manifest, _git = plugin_release
    project = plugin.source / "pyproject.toml"
    project.write_text('[project]\nname = "crewplane-ollama"\nversion = "0.2.0"\n')

    with pytest.raises(state.ReleaseError, match="required project dependency"):
        release_plugins(context)


def test_npm_publication_requires_the_default_plugin(plugin_release, monkeypatch):
    context, plugin, manifest, git = plugin_release
    runner = FakeRunner()

    def registry(package):
        if package.package_name == plugin.context.package_name:
            return state.PypiRelease(False, "", {})
        return matching_pypi(package, manifest)

    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "require_publish_git_state", constant(git))
    monkeypatch.setattr(publish, "query_pypi_release", registry)
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(state.NpmRelease(False, "", "", "", "", "", "", "", "")),
    )

    with pytest.raises(state.ReleaseError, match="npm publication is blocked"):
        publish.publish_npm(context.root, runner, execute=True)
    assert not runner.commands

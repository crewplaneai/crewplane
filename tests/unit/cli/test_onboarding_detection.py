import sys
from pathlib import Path

import pytest

from crewplane.cli.onboarding.detection import detect_providers
from crewplane.cli.onboarding.rendering import rendered_default_config
from crewplane.core.provider_names import known_provider_names
from tests.helpers.cli_provider import OLLAMA_PACKAGE, expose_distribution
from tests.helpers.platforms import requires_posix

pytestmark = pytest.mark.usefixtures("posix_cli_plans")


def test_ollama_package_detection_does_not_import_plugin(tmp_path, monkeypatch):
    expose_distribution(
        monkeypatch, tmp_path, OLLAMA_PACKAGE, "crewplane-ollama", "0.1.0"
    )
    monkeypatch.setitem(sys.modules, "crewplane_ollama", None)
    detections = detect_providers(rendered_default_config(), tmp_path, {}.get)
    local = next(item for item in detections if item.provider == "local")
    assert local.found
    assert "selecting local loads" in local.diagnostic


def test_missing_ollama_package_has_installation_guidance(tmp_path, monkeypatch):
    from importlib import metadata

    def missing_distribution(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", missing_distribution)
    local = next(
        item
        for item in detect_providers(rendered_default_config(), tmp_path, {}.get)
        if item.provider == "local"
    )
    assert not local.found
    assert "Install the optional crewplane-ollama" in local.diagnostic


@pytest.mark.parametrize("present", [False, True])
def test_detection_finds_opencode_without_launch(tmp_path, present) -> None:
    found = {"opencode": "/test/opencode"} if present else {}
    detections = detect_providers(rendered_default_config(), tmp_path, found.get)
    assert [item.provider for item in detections if item.found] == (
        ["opencode"] if present else []
    )


@pytest.mark.parametrize(
    "found",
    [
        {"env": "/usr/bin/env", "dsh": "/test/dsh"},
        {"env": "/usr/bin/env"},
        {},
        {"deepseek": "/test/deepseek", "env": "/usr/bin/env"},
    ],
)
def test_detection_requires_dsh_from_the_generated_deepseek_command(
    tmp_path, monkeypatch, found
) -> None:
    monkeypatch.setenv("PATH", str(Path.cwd() / "bin"))
    detections = detect_providers(rendered_default_config(), tmp_path, found.get)
    assert tuple(item.provider for item in detections) == (
        *known_provider_names(),
        "local",
    )
    assert [item.provider for item in detections if item.found] == (
        ["deepseek"] if "dsh" in found else []
    )


def test_detection_preserves_existing_family_executable_lookups(tmp_path) -> None:
    found = {
        name: f"/test/{name}" for name in known_provider_names() if name != "deepseek"
    }
    detections = detect_providers(rendered_default_config(), tmp_path, found.get)
    assert {item.provider for item in detections if item.found} == set(found)


@requires_posix
def test_detection_uses_profile_command_edits_and_env_search_context(tmp_path) -> None:
    binary = tmp_path / "native" / "renamed-pi"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    profile = rendered_default_config().replace(
        'cli_cmd: ["pi"]', "cli_cmd: [env, -C, native, PATH=., renamed-pi]"
    )
    detections = detect_providers(profile, tmp_path, {"env": "/usr/bin/env"}.get)
    assert [item.provider for item in detections if item.found] == ["pi"]

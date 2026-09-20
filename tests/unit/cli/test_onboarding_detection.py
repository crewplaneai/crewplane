import os

import pytest

from crewplane.cli.onboarding.detection import detect_providers
from crewplane.cli.onboarding.rendering import rendered_default_config
from crewplane.core.provider_names import known_provider_names


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
    monkeypatch.setenv("PATH", os.defpath)
    detections = detect_providers(rendered_default_config(), tmp_path, found.get)
    assert tuple(item.provider for item in detections) == known_provider_names()
    assert [item.provider for item in detections if item.found] == (
        ["deepseek"] if "dsh" in found else []
    )


def test_detection_preserves_existing_family_executable_lookups(tmp_path) -> None:
    found = {
        name: f"/test/{name}" for name in known_provider_names() if name != "deepseek"
    }
    detections = detect_providers(rendered_default_config(), tmp_path, found.get)
    assert {item.provider for item in detections if item.found} == set(found)


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

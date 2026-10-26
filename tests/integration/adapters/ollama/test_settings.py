import asyncio

import pytest

from tests.integration.adapters.ollama.test_availability import request_for


def test_missing_plugin_settings_use_default_without_creating_files(ollama, tmp_path):
    settings = ollama.availability.load_settings(tmp_path)

    assert settings.readiness_timeout_seconds == 900
    assert not (tmp_path / ".crewplane").exists()


@pytest.mark.parametrize(
    "content, expected", [("{}", 900), ("readiness_timeout_seconds: 120", 120)]
)
def test_plugin_settings_load_project_override(ollama, tmp_path, content, expected):
    path = tmp_path / ".crewplane" / "ollama.yml"
    path.parent.mkdir()
    path.write_text(content)

    assert (
        ollama.availability.load_settings(tmp_path).readiness_timeout_seconds
        == expected
    )


@pytest.mark.parametrize(
    "content",
    [
        "readiness_timeout_seconds: null",
        "readiness_timeout_seconds: 0",
        "readiness_timeout_seconds: -1",
        "readiness_timeout_seconds: .inf",
        "readiness_timeout_seconds: -.inf",
        "readiness_timeout_seconds: .nan",
        "readiness_timeout_seconds: invalid",
        "readiness_timeout_seconds: 1\nreadiness_timeout_seconds: 2",
        "unexpected_option: 1",
        "[]",
        "null",
        "readiness_timeout_seconds: [",
    ],
)
def test_invalid_plugin_settings_fail_before_probe(ollama, tmp_path, content):
    path = tmp_path / ".crewplane" / "ollama.yml"
    path.parent.mkdir()
    path.write_text(content)

    with pytest.raises(ValueError, match=r"Invalid Ollama settings.*ollama.yml"):
        asyncio.run(ollama.capability.check_availability(request_for(ollama, tmp_path)))

    assert not list(ollama.receipts.iterdir())


@pytest.mark.parametrize("invalid_file", ["directory", "encoding"])
def test_unreadable_plugin_settings_fail_explicitly(ollama, tmp_path, invalid_file):
    path = tmp_path / ".crewplane" / "ollama.yml"
    path.parent.mkdir()
    if invalid_file == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"\xff")

    with pytest.raises(ValueError, match=r"Cannot read Ollama settings.*ollama.yml"):
        asyncio.run(ollama.capability.check_availability(request_for(ollama, tmp_path)))

    assert not list(ollama.receipts.iterdir())

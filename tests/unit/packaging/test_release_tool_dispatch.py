from __future__ import annotations

from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

HANDLER_RESULT = 37
EXPECTED_TAG = "v1.2.3"


def _resolve_handler(
    release_script: ModuleType, handler_path: str
) -> tuple[object, str]:
    module_name, separator, function_name = handler_path.partition(".")
    if not separator:
        return release_script, module_name
    return getattr(release_script, module_name), function_name


def _mock_handler(
    release_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    handler_path: str,
) -> Mock:
    owner, function_name = _resolve_handler(release_script, handler_path)
    handler = Mock(return_value=HANDLER_RESULT)
    monkeypatch.setattr(owner, function_name, handler)
    return handler


@pytest.mark.parametrize(
    ("command", "handler_path"),
    (
        ("prepare", "build.prepare_release"),
        ("release-artifacts", "build.release_artifacts"),
        ("package-build", "build.package_build"),
        ("package-check", "build.package_check"),
        ("package-wheelhouse", "build.package_wheelhouse"),
        ("npm-pack", "build.npm_pack"),
    ),
)
def test_dispatches_build_commands(
    release_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
    handler_path: str,
) -> None:
    handler = _mock_handler(release_script, monkeypatch, handler_path)
    runner = object()

    result = release_script.dispatch(
        release_script.parse_args([command]), tmp_path, runner
    )

    assert result == 0
    handler.assert_called_once_with(tmp_path, runner)


@pytest.mark.parametrize(
    (
        "arguments",
        "handler_path",
        "forwards_runner",
        "forwards_expected_tag",
        "expected_result",
    ),
    (
        (("check",), "release_check", True, False, HANDLER_RESULT),
        (
            ("verify-complete", "--expected-tag", EXPECTED_TAG),
            "publish.verify_complete_release",
            True,
            True,
            HANDLER_RESULT,
        ),
        (
            ("github-release-plan", "--expected-tag", EXPECTED_TAG),
            "publish.print_github_release_plan",
            True,
            True,
            0,
        ),
        (("confirm",), "publish.confirm_release", False, False, 0),
        (("changelog-check",), "changelog_check", False, False, 0),
    ),
)
def test_dispatches_verification_and_miscellaneous_commands(
    release_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arguments: tuple[str, ...],
    handler_path: str,
    forwards_runner: bool,
    forwards_expected_tag: bool,
    expected_result: int,
) -> None:
    handler = _mock_handler(release_script, monkeypatch, handler_path)
    runner = object()
    expected_arguments: tuple[object, ...] = (tmp_path,)
    if forwards_runner:
        expected_arguments += (runner,)
    if forwards_expected_tag:
        expected_arguments += (EXPECTED_TAG,)

    result = release_script.dispatch(
        release_script.parse_args(list(arguments)), tmp_path, runner
    )

    assert result == expected_result
    handler.assert_called_once_with(*expected_arguments)


@pytest.mark.parametrize(
    ("command", "handler_path"),
    (
        ("publish-pypi", "publish.publish_pypi"),
        ("publish-npm", "publish.publish_npm"),
        ("finalize", "publish.finalize_release"),
    ),
)
@pytest.mark.parametrize("execute", (False, True), ids=("plan", "execute"))
def test_dispatches_publish_commands_with_execute_flag(
    release_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
    handler_path: str,
    execute: bool,
) -> None:
    handler = _mock_handler(release_script, monkeypatch, handler_path)
    runner = object()
    arguments = [command, "--execute"] if execute else [command]

    result = release_script.dispatch(
        release_script.parse_args(arguments), tmp_path, runner
    )

    assert result == HANDLER_RESULT
    handler.assert_called_once_with(tmp_path, runner, execute)


@pytest.mark.parametrize(
    ("command", "handler_path"),
    (
        ("install-smoke-pip", "smoke.install_smoke_pip"),
        ("install-smoke-uv", "smoke.install_smoke_uv"),
        ("install-smoke-pipx", "smoke.install_smoke_pipx"),
        ("install-smoke", "smoke.install_smoke"),
        ("install-script-smoke", "smoke.install_script_smoke"),
        ("npm-smoke", "smoke.npm_smoke"),
        ("brew-smoke", "smoke.brew_smoke"),
        ("install-check", "smoke.install_check"),
    ),
)
def test_dispatches_install_and_smoke_commands(
    release_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
    handler_path: str,
) -> None:
    handler = _mock_handler(release_script, monkeypatch, handler_path)
    runner = object()

    result = release_script.dispatch(
        release_script.parse_args([command]), tmp_path, runner
    )

    assert result == 0
    handler.assert_called_once_with(tmp_path, runner)

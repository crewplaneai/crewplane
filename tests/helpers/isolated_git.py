from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Never

import pytest

from crewplane.cli.run.workspace.git_source import GIT_MIN_VERSION, parse_git_version

GIT_COMMAND_TIMEOUT_SECONDS = 30
GIT_FLOOR_VERSION_TEXT = "git version 2.34.1"
_GIT_DIAGNOSTIC_LIMIT = 4_000
_GIT_CONFIG = (
    ("init.defaultBranch", "main"),
    ("core.hooksPath", "{hooks_dir}"),
    ("commit.gpgSign", "false"),
    ("tag.gpgSign", "false"),
    ("maintenance.auto", "false"),
    ("gc.auto", "0"),
    ("core.autocrlf", "false"),
    ("core.eol", "lf"),
    ("core.safecrlf", "false"),
)
_NON_GIT_ENVIRONMENT_KEYS_TO_CLEAR = (
    "GCM_INTERACTIVE",
    "SSH_ASKPASS",
    "SSH_ASKPASS_REQUIRE",
)
_CREDENTIAL_URL_PATTERN = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s]+@",
    flags=re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b([a-z0-9_.-]*(?:password|passwd|token|secret|credential|authorization)[a-z0-9_.-]*)=([^\s]+)"
)
_AUTHORIZATION_HEADER_PATTERN = re.compile(r"(?i)\b(authorization)\s*:\s*[^\r\n]+")
_SENSITIVE_OPTION_WORDS = frozenset(
    {
        "password",
        "passwd",
        "token",
        "secret",
        "credential",
        "authorization",
        "otp",
    }
)
_SENSITIVE_OPTION_COMPACTS = frozenset(
    {
        "accesstoken",
        "authtoken",
        "bearertoken",
    }
)


@dataclass(frozen=True)
class IsolatedGit:
    executable: Path
    environment: dict[str, str] = field(repr=False)
    required: bool

    def run(
        self,
        root: Path,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return _run_git_command(self.environment, root, args, check)

    def run_text(self, root: Path, *args: str) -> str:
        return self.run(root, *args).stdout.strip()

    def unavailable(self, reason: str) -> Never:
        _git_unavailable(reason, self.required)


@pytest.fixture
def isolated_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[IsolatedGit]:
    expected_floor_bin = os.environ.get("GIT_FLOOR_BIN")
    environment_root = tmp_path.with_name(f"{tmp_path.name}-git-environment")
    if environment_root.exists():
        shutil.rmtree(environment_root)
    with ExitStack() as cleanup:
        environment_root.mkdir()
        cleanup.callback(shutil.rmtree, environment_root, ignore_errors=True)
        environment = configure_isolated_git_environment(monkeypatch, environment_root)
        required = os.environ.get("CREWPLANE_REQUIRED_GIT") == "1"
        yield require_git(environment, required, expected_floor_bin)


def configure_isolated_git_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> dict[str, str]:
    for key in tuple(os.environ):
        if key.startswith("GIT_") or key in _NON_GIT_ENVIRONMENT_KEYS_TO_CLEAR:
            monkeypatch.delenv(key, raising=False)

    home = tmp_path / "home"
    xdg_config_home = tmp_path / "xdg-config"
    hooks_dir = tmp_path / "disabled-hooks"
    for directory in (home, xdg_config_home, hooks_dir):
        directory.mkdir()
    global_config = tmp_path / "empty-gitconfig"
    global_config.write_text("", encoding="utf-8")

    controlled_environment = {
        "HOME": home.as_posix(),
        "XDG_CONFIG_HOME": xdg_config_home.as_posix(),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": global_config.as_posix(),
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_AUTHOR_NAME": "Crewplane Test",
        "GIT_AUTHOR_EMAIL": "crewplane-test@example.invalid",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
        "GIT_COMMITTER_NAME": "Crewplane Test",
        "GIT_COMMITTER_EMAIL": "crewplane-test@example.invalid",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000",
        "LC_ALL": "C",
        "LANG": "C",
        "TZ": "UTC",
    }
    for key, value in controlled_environment.items():
        monkeypatch.setenv(key, value)

    rendered_config = tuple(
        (key, value.format(hooks_dir=hooks_dir.as_posix()))
        for key, value in _GIT_CONFIG
    )
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(rendered_config)))
    for index, (key, value) in enumerate(rendered_config):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)
    return dict(os.environ)


def require_git(
    environment: dict[str, str],
    required: bool,
    expected_floor_bin: str | None = None,
) -> IsolatedGit:
    floor_required = required or expected_floor_bin is not None
    executable = shutil.which("git", path=environment.get("PATH"))
    if executable is None:
        _git_unavailable("git is unavailable", floor_required)

    try:
        result = _run_git_command(environment, None, ("--version",), check=False)
    except AssertionError as error:
        _git_unavailable(str(error), floor_required)
    if result.returncode != 0:
        reason = _git_failure_message(
            ("git", "--version"),
            result.returncode,
            result.stdout,
            result.stderr,
        )
        _git_unavailable(reason, floor_required)
    version_text = result.stdout.strip()
    version = parse_git_version(version_text)
    if version is None or version < GIT_MIN_VERSION:
        _git_unavailable(
            "Git 2.34.1+ is required for test Git fixtures; "
            f"found {_redact_git_diagnostic(version_text) or 'unknown'}",
            floor_required,
        )
    resolved_executable = Path(executable).resolve()
    if expected_floor_bin is not None:
        _enforce_exact_git_floor(resolved_executable, version_text, expected_floor_bin)
    return IsolatedGit(
        executable=resolved_executable,
        environment=environment,
        required=floor_required,
    )


def _enforce_exact_git_floor(
    resolved_executable: Path,
    version_text: str,
    expected_floor_bin: str,
) -> None:
    expected_executable = (Path(expected_floor_bin) / "git").resolve()
    if resolved_executable != expected_executable:
        pytest.fail(
            "Git floor lane resolved an unexpected executable: "
            f"expected {expected_executable}, found {resolved_executable}."
        )
    if version_text != GIT_FLOOR_VERSION_TEXT:
        pytest.fail(
            "Git floor lane resolved an unexpected version: "
            f"expected {GIT_FLOOR_VERSION_TEXT!r}, found {version_text!r}."
        )


def run_git(
    root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run_git_command(dict(os.environ), root, args, check)


def run_git_text(root: Path, *args: str) -> str:
    return run_git(root, *args).stdout.strip()


def _run_git_command(
    environment: dict[str, str],
    root: Path | None,
    args: tuple[str, ...],
    check: bool,
) -> subprocess.CompletedProcess[str]:
    command = ["git"]
    if root is not None:
        command.extend(("-C", root.as_posix()))
    command.extend(args)
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise AssertionError(
            _git_failure_message(
                tuple(command),
                None,
                _decode_subprocess_output(error.stdout),
                _decode_subprocess_output(error.stderr),
                timed_out=True,
            )
        ) from None
    except OSError as error:
        raise AssertionError(
            _git_failure_message(tuple(command), None, "", str(error))
        ) from None
    if check and result.returncode != 0:
        raise AssertionError(
            _git_failure_message(
                tuple(command),
                result.returncode,
                result.stdout,
                result.stderr,
            )
        )
    return result


def _git_failure_message(
    command: tuple[str, ...],
    returncode: int | None,
    stdout: str,
    stderr: str,
    timed_out: bool = False,
) -> str:
    redacted_command = _redact_git_command(command)
    status = (
        f"timed out after {GIT_COMMAND_TIMEOUT_SECONDS}s"
        if timed_out
        else f"exit status {returncode}"
    )
    return "\n".join(
        (
            f"Git command failed ({status})",
            f"argv: {shlex.join(redacted_command)}",
            f"stdout: {_redact_git_diagnostic(stdout)}",
            f"stderr: {_redact_git_diagnostic(stderr)}",
        )
    )


def _redact_git_diagnostic(value: str) -> str:
    redacted = _CREDENTIAL_URL_PATTERN.sub(r"\g<scheme><redacted>@", value)
    redacted = _AUTHORIZATION_HEADER_PATTERN.sub(r"\1: <redacted>", redacted)
    redacted = _SENSITIVE_ASSIGNMENT_PATTERN.sub(r"\1=<redacted>", redacted)
    if len(redacted) > _GIT_DIAGNOSTIC_LIMIT:
        return f"{redacted[:_GIT_DIAGNOSTIC_LIMIT]}...<truncated>"
    return redacted


def _redact_git_command(command: tuple[str, ...]) -> tuple[str, ...]:
    redacted: list[str] = []
    redact_next_value = False
    for index, value in enumerate(command):
        if index > 0 and command[index - 1] == "-C":
            redacted.append("<repo>")
            continue
        if redact_next_value:
            redacted.append("<redacted>")
            redact_next_value = _is_sensitive_option_without_value(value)
            continue
        option, separator, _value = value.partition("=")
        if separator and _is_sensitive_option(option):
            redacted.append(f"{option}=<redacted>")
            continue
        if value.startswith("-") and _is_sensitive_option(option):
            redacted.append(value)
            redact_next_value = True
            continue
        redacted.append(_redact_git_diagnostic(value))
    if redact_next_value:
        redacted.append("<missing-value>")
    return tuple(redacted)


def _is_sensitive_option(option: str) -> bool:
    normalized = option.casefold().lstrip("-")
    words = frozenset(part for part in re.split(r"[^a-z0-9]+", normalized) if part)
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    return bool(
        words & _SENSITIVE_OPTION_WORDS or compact in _SENSITIVE_OPTION_COMPACTS
    )


def _is_sensitive_option_without_value(value: str) -> bool:
    option, separator, _value = value.partition("=")
    return value.startswith("-") and _is_sensitive_option(option) and not separator


def _decode_subprocess_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _git_unavailable(reason: str, required: bool) -> Never:
    if required:
        pytest.fail(reason, pytrace=False)
    else:
        pytest.skip(reason)
    raise AssertionError("pytest outcome did not stop the test")

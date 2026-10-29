import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.helpers import installed_package_assertions as acceptance
from tests.helpers import installed_wheel_smoke


@pytest.mark.parametrize(
    "run", [acceptance.run, installed_wheel_smoke.run], ids=["acceptance", "wheel"]
)
def test_package_runner_preserves_environment_directory_and_merged_output(
    tmp_path, run
) -> None:
    environment = {**os.environ, "CREWPLANE_SMOKE_INPUT": "value"}
    command = [
        sys.executable,
        "-c",
        "import os, sys; assert os.getcwd() == sys.argv[1]; "
        "os.write(1, os.environ['CREWPLANE_SMOKE_INPUT'].encode() + b'\\xff'); "
        "os.write(2, b'err\\xfe')",
        str(tmp_path),
    ]

    assert run(command, tmp_path, environment) == "value\ufffderr\ufffd"


@pytest.mark.parametrize(
    "run", [acceptance.run, installed_wheel_smoke.run], ids=["acceptance", "wheel"]
)
def test_package_runner_preserves_nonzero_exit_diagnostics(tmp_path, run) -> None:
    command = [
        sys.executable,
        "-c",
        "import os, sys; os.write(2, b'failed\\xff'); sys.exit(7)",
    ]

    with pytest.raises(RuntimeError) as caught:
        run(command, tmp_path, dict(os.environ))

    assert str(caught.value) == f"Command failed (7): {command}\nfailed\ufffd"


@pytest.mark.parametrize(
    "run", [acceptance.run, installed_wheel_smoke.run], ids=["acceptance", "wheel"]
)
def test_package_runner_propagates_timeout(tmp_path, monkeypatch, run) -> None:
    command, environment = ["provider"], {"INPUT": "value"}
    failure = subprocess.TimeoutExpired(command, 180)
    launch = Mock(side_effect=failure)
    monkeypatch.setattr(acceptance.subprocess, "run", launch)

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run(command, tmp_path, environment)

    assert caught.value is failure
    launch.assert_called_once_with(
        command,
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
        check=False,
    )


@pytest.mark.parametrize("mode", ["package", "script"])
def test_wheel_smoke_imports_in_both_launch_modes(tmp_path, mode) -> None:
    script = Path(installed_wheel_smoke.__file__)
    arguments = (
        ["-m", "tests.helpers.installed_wheel_smoke"]
        if mode == "package"
        else [str(script)]
    )
    result = subprocess.run(
        [sys.executable, *arguments, "--help"],
        cwd=script.parents[2] if mode == "package" else tmp_path,
        env=acceptance.package_environment(tmp_path / "environment"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=True,
    )

    assert "--installer {pip,uv}" in result.stdout


@pytest.mark.parametrize(
    "invalid",
    [
        "version",
        "checkout-origin",
        "outside-prefix",
        "resources",
        "missing-plugin",
        "plugin-checkout-origin",
    ],
)
def test_acceptance_rejects_invalid_installations_before_cli_launch(
    tmp_path, monkeypatch, invalid
) -> None:
    checkout = tmp_path / "checkout"
    prefix = tmp_path / "installed"
    metadata = {
        "version": "1.2.3",
        "origin": str(prefix / "crewplane/__init__.py"),
        "prefix": str(prefix),
        "config_resource": True,
        "default_workflow_resource": True,
        "library_resources": 1,
        "ollama_version": "0.1.0",
        "ollama_origin": str(prefix / "crewplane_ollama/__init__.py"),
    }
    if invalid == "version":
        metadata["version"] = "0.0.0"
    elif invalid == "checkout-origin":
        metadata["origin"] = str(checkout / "src/crewplane/__init__.py")
    elif invalid == "outside-prefix":
        metadata["origin"] = str(tmp_path / "ambient/crewplane/__init__.py")
    elif invalid == "resources":
        metadata["library_resources"] = 0
    elif invalid == "missing-plugin":
        metadata["ollama_version"] = ""
    else:
        metadata["ollama_origin"] = str(
            checkout / "plugins/crewplane_ollama/__init__.py"
        )

    def inspect_installation(*args):
        del args
        return metadata

    def reject_launch(*args):
        del args
        raise RuntimeError("invalid installation reached CLI launch")

    monkeypatch.setattr(acceptance, "inspect_installation", inspect_installation)
    monkeypatch.setattr(acceptance, "run", reject_launch)
    with pytest.raises(AssertionError):
        acceptance.smoke(
            prefix / "python",
            prefix / "crewplane",
            tmp_path / "project",
            "1.2.3",
            checkout,
            tmp_path / "diagnostics",
        )
    assert (
        json.loads((tmp_path / "diagnostics/installed-environment.json").read_text())
        == metadata
    )


@pytest.mark.parametrize("corruption", ["size", "hash"])
def test_shared_artifact_assertions_reject_corrupt_output(
    tmp_path: Path, corruption: str
) -> None:
    stage = tmp_path / ".crewplane/execution-stages/run-1"
    manifests = stage / "manifests"
    (manifests / "nodes").mkdir(parents=True)
    results = tmp_path / ".crewplane/execution-results/run-1"
    results.mkdir(parents=True)
    payload = b"expected"
    (results / "node.md").write_bytes(payload)
    (stage / "summary.md").touch()
    (manifests / "run.json").write_text('{"status": "succeeded"}')
    descriptor = {
        "relative_path": "node.md",
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    descriptor["size_bytes" if corruption == "size" else "sha256"] = (
        0 if corruption == "size" else "0" * 64
    )
    (manifests / "nodes/node.json").write_text(json.dumps({"artifacts": [descriptor]}))
    with pytest.raises(AssertionError):
        acceptance.verify_run(tmp_path, stage)


def test_package_acceptance_removes_checkout_and_measurement_environment(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "checkout/src")
    monkeypatch.setenv("COVERAGE_PROCESS_CONFIG", "measurement only")
    monkeypatch.setenv("COV_CORE_SOURCE", "old measurement")
    monkeypatch.setenv("OLLAMA_HOST", "http://ambient.invalid:11434")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-credential-sentinel")
    external = tmp_path / "external-cache"
    external.mkdir()
    monkeypatch.setenv("XDG_CACHE_HOME", str(external))
    original_path = os.environ.get("PATH")
    environment = acceptance.package_environment(
        tmp_path / "environment", "restricted-tools"
    )
    tool_path = environment["PATH"]
    parent_path = os.environ.get("PATH")
    has_host = "OLLAMA_HOST" in environment
    has_api_key = "OPENAI_API_KEY" in environment
    assert tool_path == "restricted-tools"
    assert parent_path == original_path
    assert not has_host
    assert not has_api_key
    cache = Path(environment["XDG_CACHE_HOME"])
    assert cache == tmp_path / "environment/xdg-cache"
    (cache / "receipt").write_text("owned", encoding="utf-8")
    assert not list(external.iterdir())
    has_python_path = "PYTHONPATH" in environment
    has_coverage = any(
        key.startswith(("COVERAGE_", "COV_CORE_")) for key in environment
    )
    assert not has_python_path
    assert not has_coverage


@pytest.mark.parametrize(
    ("wait_result", "running"),
    [
        pytest.param(0, False, id="exited"),
        pytest.param(258, True, id="running"),
        pytest.param(0xFFFFFFFF, None, id="wait-failed"),
    ],
)
def test_native_process_probe_distinguishes_exit_from_wait_failure(
    monkeypatch, wait_result, running
) -> None:
    import ctypes

    api = SimpleNamespace(
        OpenProcess=Mock(return_value=123),
        WaitForSingleObject=Mock(restype=ctypes.c_int),
        CloseHandle=Mock(return_value=1),
    )

    def native_wait(handle, timeout):
        assert (handle, timeout) == (123, 0)
        return api.WaitForSingleObject.restype(wait_result).value

    api.WaitForSingleObject.side_effect = native_wait
    monkeypatch.setattr(acceptance, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=api), raising=False)
    monkeypatch.setattr(
        ctypes, "WinError", Mock(return_value=OSError("wait failed")), raising=False
    )
    if running is None:
        with pytest.raises(OSError, match="wait failed"):
            acceptance.process_running(456)
    else:
        assert acceptance.process_running(456) is running
    api.CloseHandle.assert_called_once_with(123)


@pytest.mark.parametrize("error", [87, 5], ids=["missing-process", "access-denied"])
def test_native_process_probe_propagates_unexpected_open_failure(
    monkeypatch, error
) -> None:
    import ctypes

    api = SimpleNamespace(
        OpenProcess=Mock(return_value=0),
        WaitForSingleObject=Mock(),
        CloseHandle=Mock(),
    )
    monkeypatch.setattr(acceptance, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=api), raising=False)
    monkeypatch.setattr(
        ctypes, "get_last_error", Mock(return_value=error), raising=False
    )
    monkeypatch.setattr(
        ctypes, "WinError", Mock(return_value=OSError("access denied")), raising=False
    )
    if error == 87:
        assert acceptance.process_running(456) is False
    else:
        with pytest.raises(OSError, match="access denied"):
            acceptance.process_running(456)
    api.WaitForSingleObject.assert_not_called()
    api.CloseHandle.assert_not_called()

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.helpers import installed_package_assertions as acceptance


@pytest.mark.parametrize(
    "invalid", ["version", "checkout-origin", "outside-prefix", "resources"]
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
    }
    if invalid == "version":
        metadata["version"] = "0.0.0"
    elif invalid == "checkout-origin":
        metadata["origin"] = str(checkout / "src/crewplane/__init__.py")
    elif invalid == "outside-prefix":
        metadata["origin"] = str(tmp_path / "ambient/crewplane/__init__.py")
    else:
        metadata["library_resources"] = 0

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
) -> None:
    monkeypatch.setenv("PYTHONPATH", "checkout/src")
    monkeypatch.setenv("COVERAGE_PROCESS_CONFIG", "measurement only")
    monkeypatch.setenv("COV_CORE_SOURCE", "old measurement")
    environment = acceptance.package_environment()
    assert "PYTHONPATH" not in environment
    assert not any(key.startswith(("COVERAGE_", "COV_CORE_")) for key in environment)


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

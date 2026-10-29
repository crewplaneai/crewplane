"""Exercise the real launch boundary before collection and in workers/children."""

import json
import shutil
from pathlib import Path

import pytest

from tests.helpers.fixture_files import copy_fixture
from tests.helpers.test_runner_project import ROOT, run_fixture, runner_project


def isolation_project(root: Path, host: str | None, fail: bool = False) -> Path:
    root.mkdir()
    script = runner_project(root)
    shutil.copyfile(
        ROOT / "tests/helpers/isolated_git.py", root / "tests/helpers/isolated_git.py"
    )
    shutil.copyfile(
        ROOT / "tests/conftest.py", root / "tests/helpers/pytest_environment.py"
    )
    copy_fixture("test_runner/environment_startup.py", root / "conftest.py")
    copy_fixture("test_runner/environment_case.py", root / "test_environment.py")
    (root / "expected.json").write_text(
        json.dumps({"host": host, "fail": fail}), encoding="utf-8"
    )
    return script


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "workers"])
def test_pollution_cannot_change_collection_or_escape_owned_directories(
    tmp_path, workers
):
    external = tmp_path / "external-cache"
    external.mkdir()
    imports = tmp_path / "ambient-imports"
    imports.mkdir()
    (imports / "ambient_only_module.py").write_text("value = 1\n", encoding="utf-8")
    pollution = {
        "OLLAMA_HOST": "http://ambient.invalid:11434",
        "XDG_CACHE_HOME": str(external),
        "PYTHONPATH": str(imports),
        "PYTEST_ADDOPTS": "-k no_tests_should_match",
        "PYTEST_PLUGINS": "missing_ambient_plugin",
        "OPENAI_API_KEY": "synthetic-credential-sentinel",
    }
    for label, ambient in (("controlled", {}), ("polluted", pollution)):
        root = tmp_path / label
        script = isolation_project(root, None)
        result = run_fixture(script, "--workers", str(workers), pollution=ambient)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "4 passed" in result.stdout
        receipts = [
            json.loads(path.read_text()) for path in sorted(root.glob("receipt-*.json"))
        ]
        assert [receipt["case"] for receipt in receipts] == [0, 1, 2, 3]
        assert len({receipt["home"] for receipt in receipts}) == 4
        assert len({receipt["cache"] for receipt in receipts}) == 4
        assert len({receipt["collection_home"] for receipt in receipts}) == (
            2 if workers else 1
        )
        (diagnostics,) = (root / "test-results").iterdir()
        metadata = json.loads((diagnostics / "metadata.json").read_text())
        execution_root = Path(
            metadata["execution_environment"]["CREWPLANE_TEST_ENV_ROOT"]
        )
        assert not execution_root.is_relative_to(ROOT)
        for receipt in receipts:
            for key in ("home", "cache", "collection_home", "collection_cache"):
                assert Path(receipt[key]).is_relative_to(execution_root)
        assert metadata["workers"] == workers
        assert metadata["execution_environment"]["TZ"] == "UTC"
        assert not execution_root.exists()
        for path in diagnostics.rglob("*"):
            if path.is_file():
                assert "synthetic-credential-sentinel" not in path.read_text(
                    errors="replace"
                )
        assert "synthetic-credential-sentinel" not in result.stdout + result.stderr
    assert not list(external.iterdir())


def test_explicit_inheritance_and_failed_evidence_survive_cleanup(tmp_path):
    host = "http://scenario.invalid:11434"
    root = tmp_path / "scenario"
    script = isolation_project(root, host, fail=True)
    result = run_fixture(script, scenario={"OLLAMA_HOST": host})
    assert result.returncode == 1
    assert "original scenario failure" in result.stdout
    (diagnostics,) = (root / "test-results").iterdir()
    outcome = json.loads((diagnostics / "outcome.json").read_text())
    assert outcome["pytest"]["returncode"] == outcome["exit_code"] == 1
    assert (diagnostics / "pytest.log").is_file()
    assert (diagnostics / "junit.xml").is_file()
    receipt = json.loads((root / "receipt-0.json").read_text())
    assert receipt["child_host"] == host
    assert (Path(receipt["cache"]) / "test-write").read_text() == "owned"

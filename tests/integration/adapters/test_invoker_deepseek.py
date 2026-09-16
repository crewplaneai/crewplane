import asyncio
import errno
import json
import sys

import pytest

from crewplane.adapters.invokers.cli import CliInvokerAdapter
from crewplane.core.config import AgentConfig, Config
from crewplane.version import SCHEMA_VERSION


@pytest.fixture
def native_config(tmp_path):
    harness = tmp_path / "dsh-double"
    harness.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "Path('captured.json').write_text(json.dumps(sys.argv[1:]))\n"
        "scenario = json.loads(Path('scenario.json').read_text())\n"
        "time.sleep(scenario.get('delay', 0))\n"
        "print(scenario.get('stderr', ''), file=sys.stderr)\n"
        "print(scenario.get('stdout', sys.argv[-1]), end='')\n"
        "sys.exit(scenario.get('exit', 0))\n"
    )
    harness.chmod(0o755)
    (tmp_path / "scenario.json").write_text("{}")
    return AgentConfig(
        cli_cmd=[
            "env",
            "DSH_PERMISSION_MODE=danger-full-access",
            str(harness),
            "--profile",
            "headless",
        ],
        provider_kind="deepseek",
        prompt_transport="argv",
        prompt_transport_arg="--",
    )


def invoke(config, prompt, tmp_path):
    invoker = CliInvokerAdapter().create_invoker(
        Config(version=SCHEMA_VERSION, agents={"deepseek": config})
    )
    return invoker.invoke(
        config,
        None,
        prompt,
        tmp_path / "answer.md",
        tmp_path,
        log_file=tmp_path / "native.log",
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "--help",
        "-leading",
        "first line\nsecond line",
        "'single' and \"double\" quotes",
        "ordinary text λ",
        "line one\n'line two' λ",
    ],
)
def test_deepseek_real_subprocess_preserves_one_literal_prompt(
    native_config, tmp_path, prompt
) -> None:
    asyncio.run(invoke(native_config, prompt, tmp_path))
    assert json.loads((tmp_path / "captured.json").read_text()) == [
        "--profile",
        "headless",
        "--",
        "--",
        prompt,
    ]
    assert (tmp_path / "answer.md").read_text() == prompt


@pytest.mark.parametrize("exit_code, stdout", [(0, " \n"), (3, "partial answer")])
def test_deepseek_preserves_permission_diagnostics_without_publishing(
    native_config, tmp_path, exit_code, stdout
) -> None:
    diagnostic = "permission denied: tool approval request rejected by policy never"
    (tmp_path / "scenario.json").write_text(
        json.dumps({"exit": exit_code, "stdout": stdout, "stderr": diagnostic})
    )
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(invoke(native_config, "prompt", tmp_path))
    assert not (tmp_path / "answer.md").exists()
    assert diagnostic in (tmp_path / "native.log").read_text()
    if exit_code:
        assert "permission denied" in str(caught.value)


def test_deepseek_missing_executable_is_explicit(
    native_config, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DSH_PERMISSION_MODE", "danger-full-access")
    native_config.cli_cmd = ["crewplane-missing-dsh", "--profile", "headless"]
    with pytest.raises(RuntimeError, match="CLI executable not found"):
        asyncio.run(invoke(native_config, "prompt", tmp_path))
    assert not (tmp_path / "answer.md").exists()


def test_deepseek_argv_limit_failure_has_no_fallback_or_truncation(
    native_config, tmp_path, monkeypatch
) -> None:
    prompt = "large literal prompt " * 100_000
    calls = []

    async def fail_launch(*args, **kwargs):
        assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
        assert args[-3:] == ("--", "--", prompt)
        calls.append(args)
        raise OSError(errno.E2BIG, "Argument list too long")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_launch)
    with pytest.raises(RuntimeError, match="Argument list too long") as caught:
        asyncio.run(invoke(native_config, prompt, tmp_path))
    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.__cause__.errno == errno.E2BIG
    assert len(calls) == 1
    assert not (tmp_path / "captured.json").exists()
    assert not (tmp_path / "answer.md").exists()


@pytest.mark.parametrize(
    "timeout_field", ["invocation_timeout_seconds", "invocation_idle_timeout_seconds"]
)
def test_deepseek_uses_shared_configured_timeouts(
    native_config, tmp_path, timeout_field
) -> None:
    (tmp_path / "scenario.json").write_text('{"delay": 30}')
    setattr(native_config, timeout_field, 0.1)
    with pytest.raises(RuntimeError, match="[Tt]imeout|timed out|produced no output"):
        asyncio.run(invoke(native_config, "prompt", tmp_path))
    assert not (tmp_path / "answer.md").exists()

import asyncio
import json
import tempfile
from pathlib import Path
from threading import Event

import pytest

from crewplane.adapters.invokers.cli import CliInvokerAdapter
from crewplane.adapters.invokers.cli_invoker import build_cli_invocation_plan
from crewplane.adapters.invokers.cli_invoker.providers.opencode import OPENCODE
from crewplane.architecture.contracts import (
    CommandResult,
    InvocationContext,
    InvocationSourceContext,
    InvocationWorkspaceContext,
    InvocationWorktreeContract,
)
from crewplane.core.config import AgentConfig, Config
from crewplane.core.preflight.execution_nodes import resolve_provider_model
from crewplane.core.workflow.models import ProviderSpec
from crewplane.runtime.agent.invoker import invoke_agent_with_runner
from crewplane.version import SCHEMA_VERSION
from tests.helpers.opencode import (
    event,
    finish_event,
    fixture_text,
    native_tokens,
    stream,
    text_event,
    write_fake_executable,
)


@pytest.fixture
def native_config(tmp_path):
    executable = write_fake_executable(tmp_path)
    (tmp_path / "scenarios.json").write_text(json.dumps([{"stdout": fixture_text()}]))
    return AgentConfig(
        cli_cmd=[str(executable), "run"],
        provider_kind="opencode",
        retry_delay_seconds=0,
        quota_reached_retry_delay_seconds=0,
        quota_reset_sleep_floor_seconds=0,
        quota_retry_max_attempts=1,
    )


@pytest.fixture
def capture_files(tmp_path, monkeypatch):
    allocated = []
    original = tempfile.mkstemp

    def allocate(*args, **kwargs):
        descriptor, name = original(*args, **(kwargs | {"dir": tmp_path}))
        allocated.append(Path(name))
        return descriptor, name

    monkeypatch.setattr(tempfile, "mkstemp", allocate)
    return allocated


def invoke(config, directory, context=None, prompt="prompt", model=None):
    invoker = CliInvokerAdapter().create_invoker(
        Config(version=SCHEMA_VERSION, agents={"local-worker": config})
    )
    return invoker.invoke(
        config,
        model,
        prompt,
        directory / "answer.md",
        directory,
        log_file=directory / "provider.log",
        invocation_context=context,
    )


def context_with_usage(usages, **fields):
    return InvocationContext(
        "node",
        "task",
        "local-worker",
        "executor",
        usage_recorder=usages.append,
        **fields,
    )


@pytest.mark.parametrize(
    "default,override,expected",
    [
        (None, None, None),
        ("provider/default", None, "provider/default"),
        ("provider/default", "vendor/workflow/model", "vendor/workflow/model"),
    ],
)
def test_child_receives_literal_stdin_resolved_model_arguments_and_cwd(
    native_config, tmp_path, monkeypatch, capture_files, default, override, expected
):
    monkeypatch.setenv("OPENCODE_TEST_ENV", "inherited")
    monkeypatch.setenv("PWD", str(tmp_path.parent))
    native_config.default_model = default
    native_config.extra_args = ["--variant", "high", "--thinking=false"]
    model = resolve_provider_model(
        ProviderSpec(provider="local-worker", model=override), native_config
    )
    prompt = "--help\n\"double\" 'single' λ 中文\n"
    usages = []
    asyncio.run(
        invoke(native_config, tmp_path, context_with_usage(usages), prompt, model)
    )
    calls = [
        json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()
    ]
    assert calls == [
        {
            "argv": [
                "run",
                *(["--model", expected] if expected else []),
                "--variant",
                "high",
                "--thinking=false",
                "--format",
                "json",
                "--dir",
                str(tmp_path),
            ],
            "stdin": prompt,
            "cwd": str(tmp_path),
            "env": "inherited",
            "pwd": str(tmp_path.parent),
            "session_directory": str(tmp_path),
        }
    ]
    assert (tmp_path / "answer.md").read_text() == "Answer λ\n"
    assert fixture_text() in (tmp_path / "provider.log").read_text()
    assert len(capture_files) == 2
    assert all(not path.exists() for path in capture_files)
    assert usages[0].provider_tokens["total"] == 25
    assert usages[0].attempt_count == usages[0].provider_usage_report_count == 1


@pytest.mark.parametrize("workspace_environment", [False, True])
def test_native_session_uses_runtime_directory_despite_inherited_pwd(
    native_config, tmp_path, monkeypatch, workspace_environment
):
    original_checkout = tmp_path / "original checkout"
    original_checkout.mkdir()
    monkeypatch.setenv("PWD", str(original_checkout))
    context = None
    if workspace_environment:
        context = context_with_usage(
            [],
            workspace=InvocationWorkspaceContext(
                workspace_kind="snapshot",
                materialization="snapshot_checkout",
                logical_worktree_name="primary",
                cwd=tmp_path,
                invocation_source=InvocationSourceContext(
                    source_kind="project",
                    source_node_id=None,
                    source_commit="a" * 40,
                    source_tree="b" * 40,
                ),
                worktree_contract=InvocationWorktreeContract(
                    mode="blob_exact", schema_version=SCHEMA_VERSION
                ),
                child_environment_required=True,
            ),
        )
    asyncio.run(invoke(native_config, tmp_path, context))
    call = json.loads((tmp_path / "calls.jsonl").read_text())
    assert call["cwd"] == str(tmp_path)
    assert call["pwd"] == str(original_checkout)
    assert call["session_directory"] == str(tmp_path)
    assert (tmp_path / "answer.md").read_text() == "Answer λ\n"


def test_logs_retain_tool_events_and_diagnostics_but_answer_is_final_text(
    native_config, tmp_path
):
    (tmp_path / "scenarios.json").write_text(
        json.dumps(
            [{"stdout": fixture_text("multistep"), "stderr": "native diagnostic"}]
        )
    )
    asyncio.run(invoke(native_config, tmp_path))
    assert (tmp_path / "answer.md").read_text() == "Final answer\nSecond part\n"
    log = (tmp_path / "provider.log").read_text()
    assert "native diagnostic" in log
    assert "tool transcript" in log
    assert '"type": "step_finish"' in log


@pytest.mark.parametrize(
    "scenario,status",
    [
        ({"stdout": fixture_text(), "exit": 3}, "missing"),
        (
            {
                "stdout": fixture_text()
                + stream(
                    {
                        "type": "error",
                        "error": {"message": "session failed", "isRetryable": True},
                    }
                )
            },
            "malformed",
        ),
        ({"stdout": stream(text_event())}, "missing"),
        ({"stdout": stream(text_event(), finish_event("length"))}, "missing"),
        ({"stderr": fixture_text()}, "missing"),
        ({"stdout": "not-json"}, "malformed"),
    ],
)
def test_failed_or_incomplete_attempts_do_not_publish(
    native_config, tmp_path, capture_files, scenario, status
):
    scenario = {"stderr": "native diagnostic", **scenario}
    (tmp_path / "scenarios.json").write_text(json.dumps([scenario]))
    usages = []
    with pytest.raises(RuntimeError):
        asyncio.run(invoke(native_config, tmp_path, context_with_usage(usages)))
    assert not (tmp_path / "answer.md").exists()
    log = (tmp_path / "provider.log").read_text()
    assert all(line in log for line in scenario["stderr"].splitlines())
    assert len(capture_files) == 2
    assert all(not path.exists() for path in capture_files)
    assert len(usages) == 1
    assert usages[0].attempt_count == 1
    assert usages[0].output_extraction_status == status


def test_malformed_usage_remains_telemetry_only(native_config, tmp_path):
    stdout = stream(text_event(), finish_event(tokens=native_tokens(input="invalid")))
    (tmp_path / "scenarios.json").write_text(json.dumps([{"stdout": stdout}]))
    usages = []
    asyncio.run(invoke(native_config, tmp_path, context_with_usage(usages)))
    assert (tmp_path / "answer.md").read_text() == "Answer λ\n"
    assert usages[0].output_extraction_status == "success"
    assert usages[0].provider_usage_status == "malformed"
    assert usages[0].usage_parse_error is not None


@pytest.mark.parametrize("retry", ["answer", "quota"])
def test_configured_retry_paths_aggregate_attempts_and_clean_streams(
    native_config, tmp_path, capture_files, retry
):
    native_config.max_retries = 1
    native_config.retry_on_output_contains = ["please retry"]
    first = (
        stream(text_event("please retry"), finish_event(tokens=native_tokens()))
        if retry == "answer"
        else stream(
            finish_event("error", tokens=native_tokens()),
            {"type": "error", "error": {"message": "rate limit reached"}},
        )
    )
    (tmp_path / "scenarios.json").write_text(
        json.dumps([{"stdout": first}, {"stdout": fixture_text()}])
    )
    usages, diagnostics, resets = [], [], []

    def reset():
        resets.append(True)

    context = context_with_usage(
        usages, diagnostics=diagnostics.append, retry_reset=reset
    )
    asyncio.run(invoke(native_config, tmp_path, context))
    assert (tmp_path / "answer.md").read_text() == "Answer λ\n"
    assert len((tmp_path / "calls.jsonl").read_text().splitlines()) == 2
    assert len(capture_files) == 4
    assert all(not path.exists() for path in capture_files)
    assert resets == [True]
    assert len(usages) == 1
    assert usages[0].attempt_count == 2
    assert usages[0].provider_usage_report_count == 2
    assert usages[0].provider_tokens["total"] == 50
    assert [item.operation for item in diagnostics] == [
        "quota_retry_scheduled" if retry == "quota" else "retry_scheduled"
    ]
    log = (tmp_path / "provider.log").read_text()
    assert first in log and fixture_text() in log


def test_quota_looking_successful_tool_content_does_not_retry(native_config, tmp_path):
    native_config.quota_reached_on_contains = ["quota reached"]
    stdout = stream(
        event(
            "tool_use",
            "tool-1",
            state={
                "status": "completed",
                "output": "quota reached; rate limit reached",
            },
        ),
        text_event(),
        finish_event(tokens=native_tokens()),
    )
    (tmp_path / "scenarios.json").write_text(json.dumps([{"stdout": stdout}]))
    usages = []
    asyncio.run(invoke(native_config, tmp_path, context_with_usage(usages)))
    assert usages[0].attempt_count == 1
    assert (tmp_path / "answer.md").read_text() == "Answer λ\n"
    assert "quota reached" in (tmp_path / "provider.log").read_text()


def test_cancellation_during_retry_releases_borrowed_streams(tmp_path):
    stdout, stderr = tmp_path / "stdout", tmp_path / "stderr"
    config = AgentConfig(
        cli_cmd=["opencode", "run"],
        provider_kind="opencode",
        max_retries=1,
        retry_on_output_contains=["retry"],
        retry_delay_seconds=0,
    )
    captures = []

    async def runner(**kwargs):
        assert kwargs["cmd"][1:] == [
            "run",
            "--format",
            "json",
            "--dir",
            str(tmp_path),
        ]
        stdout.write_text(
            stream(text_event("retry"), finish_event(tokens=native_tokens()))
        )
        stderr.write_text("diagnostic")
        result = CommandResult(0, "", "", stdout, stderr)
        assert OPENCODE.output_extractor(result, None).output_text == "retry\n"
        assert OPENCODE.usage_decoder(result).valid_report_count == 1
        assert stdout.exists() and stderr.exists()
        captures.append(result)
        return result

    async def cancel_at_reset():
        resetting = asyncio.Event()
        released = Event()
        loop = asyncio.get_running_loop()

        def reset():
            loop.call_soon_threadsafe(resetting.set)
            assert released.wait(5)

        task = asyncio.create_task(
            invoke_agent_with_runner(
                config,
                None,
                "prompt",
                tmp_path / "answer.md",
                tmp_path,
                None,
                context_with_usage(
                    [], retry_reset=reset, retry_reset_canceller=released.set
                ),
                runner,
                build_cli_invocation_plan,
            )
        )
        try:
            await asyncio.wait_for(resetting.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            released.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(cancel_at_reset())
    assert len(captures) == 1
    assert list(tmp_path.iterdir()) == []

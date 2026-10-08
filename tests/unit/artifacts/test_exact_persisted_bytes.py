import hashlib

import pytest

from crewplane.architecture.contracts import OutputExtractionResult
from crewplane.artifacts.generated_files import snapshot_metadata
from crewplane.artifacts.generated_files.catalog import (
    snapshot_generated_file_workspace,
)
from crewplane.artifacts.manager import OutputManager
from crewplane.observability.events import (
    format_execution_event_log_line,
    runtime_log_event,
)
from crewplane.observability.run_summary.logger import PersistentRunLogger
from crewplane.observability.types import RunContext, WorkflowTopology
from crewplane.runtime.agent.invocation.output import write_extracted_invocation_output
from crewplane.runtime.agent.invocation.state import ExtractedInvocationOutput
from tests.helpers.artifacts import node_artifact_request


@pytest.mark.parametrize(
    "payload",
    [b"LF\n", b"CRLF\r\n\x1a\xff", "日本語 🌍\r\n".encode() * 200000],
    ids=["lf", "crlf-ctrlz-invalid-utf8", "multichunk-unicode"],
)
def test_provider_file_backed_output_is_byte_preserving(tmp_path, payload) -> None:
    source = tmp_path / "provider"
    source.write_bytes(payload)
    output = tmp_path / "output"
    write_extracted_invocation_output(
        ExtractedInvocationOutput(
            OutputExtractionResult("", "success", output_path=source)
        ),
        output,
    )
    assert output.read_bytes() == payload


@pytest.mark.parametrize("payload", [b"caf\xe9\r\n", b"CRLF\r\n\x1a\xff"])
def test_byte_preserving_output_finalizes_with_tolerant_text(tmp_path, payload) -> None:
    source = tmp_path / "provider"
    source.write_bytes(payload)
    artifacts = OutputManager("bytes", base_dir=tmp_path / "state")
    request = node_artifact_request("build")
    stage = artifacts.create_node_dir(request)
    output = stage / "generic_round1.md"
    write_extracted_invocation_output(
        ExtractedInvocationOutput(
            OutputExtractionResult("", "success", output_path=source)
        ),
        output,
    )

    result = artifacts.finalize_node(request)

    assert output.read_bytes() == payload
    assert result.included_outputs == (output,)
    assert payload.decode("utf-8", errors="replace").strip() in (
        result.result_file.read_bytes().decode("utf-8")
    )


def test_invalid_utf8_output_still_captures_claimed_files(tmp_path) -> None:
    generated = tmp_path / "generated.txt"
    generated.write_bytes(b"raw\r\n\x1a\xff")
    output = tmp_path / "output.md"
    payload = b"caf\xe9\r\nUpdated `generated.txt`.\r\n"
    output.write_bytes(payload)

    snapshot = snapshot_generated_file_workspace(output, tmp_path)

    assert output.read_bytes() == payload
    assert (snapshot / "generated.txt").read_bytes() == generated.read_bytes()


def test_extracted_text_and_event_records_do_not_translate_newlines(tmp_path) -> None:
    value = "LF\nCRLF\r\nCtrl-Z\x1a café"
    output = tmp_path / "output"
    write_extracted_invocation_output(
        ExtractedInvocationOutput(OutputExtractionResult(value, "success")), output
    )
    assert output.read_bytes() == value.encode()
    artifacts = OutputManager("bytes", base_dir=tmp_path / "state")
    logger = PersistentRunLogger(artifacts)
    logger.start(
        RunContext(
            WorkflowTopology(workflow_name="bytes", nodes=()), artifacts.run_id, 0
        )
    )
    event = runtime_log_event(
        workflow_name="bytes",
        run_id=artifacts.run_id,
        level="warning",
        message=value,
        operation="test",
    )
    logger.record_event(event)
    assert (
        artifacts.get_run_event_log_path().read_bytes()
        == format_execution_event_log_line(event).encode()
    )


def test_generated_file_metadata_hashes_written_bytes(tmp_path) -> None:
    source = tmp_path / "café"
    signature = snapshot_metadata.write_source_metadata(tmp_path, source)
    payload = (tmp_path / ".crewplane-generated-file-source.json").read_bytes()
    assert signature == (len(payload), hashlib.sha256(payload).hexdigest())
    assert payload.endswith(b"\n") and not payload.endswith(b"\r\n")

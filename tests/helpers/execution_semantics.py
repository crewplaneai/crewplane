"""Reproduce pre-fix signature payloads exclusively in test fixtures."""

from contextlib import contextmanager
from unittest.mock import patch

from crewplane.core.preflight import plan_signatures
from crewplane.core.preflight.signatures import signature_for_payload


@contextmanager
def pre_fix_execution_semantics():
    def old_signature(payload):
        if isinstance(payload, dict):
            payload = {
                key: value
                for key, value in payload.items()
                if key != "execution_semantics_revision"
            }
        return signature_for_payload(payload)

    with patch.object(plan_signatures, "signature_for_payload", new=old_signature):
        yield


def compiled_execution_signatures(root):
    from rich.console import Console

    from crewplane.bootstrap import build_runtime_config_snapshot
    from crewplane.core.preflight import (
        PreflightCompileOptions,
        compile_preflight_preview,
    )
    from tests.integration.architecture.preflight_plan.helpers import (
        literal_workflow,
        make_source,
        mock_config,
    )

    config = mock_config()
    snapshot = build_runtime_config_snapshot(config, Console(), True).snapshot
    options = PreflightCompileOptions(
        project_root=root,
        state_dir=root / ".crewplane",
        fingerprint_key_policy="read_only",
    )

    def compile_signature():
        preview = compile_preflight_preview(
            make_source(literal_workflow()), config, snapshot, options
        )
        assert not preview.diagnostics
        assert preview.workflow_signature
        return preview.workflow_signature

    current = compile_signature()
    with pre_fix_execution_semantics():
        previous = compile_signature()
    return current, previous

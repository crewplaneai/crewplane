from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from threading import Lock

from pydantic import ValidationError

from crewplane.architecture.contracts import InvocationProcessEvent
from crewplane.architecture.ports.artifacts import (
    ProviderProcessInvocation,
    ProviderProcessPublication,
)
from crewplane.architecture.safe_files import (
    ensure_contained_directory,
    is_single_link_regular_file,
)
from crewplane.core.execution_state import RUN_STATE_SCHEMA_VERSION
from crewplane.core.provider_process_state import ProviderProcessState

from .atomic import (
    atomic_write_bytes,
    atomic_write_bytes_if_absent,
    json_bytes,
)
from .directory_manager import DirectoryManager
from .locks.process_identity import ProcessInspector
from .naming import build_provider_process_state_filename


class ProviderProcessPublisher:
    """Publish synchronized provider-process receipts for one workflow run."""

    def __init__(self, directories: DirectoryManager) -> None:
        self._directories = directories
        self._expected_states: dict[Path, ProviderProcessState] = {}
        self._lock = Lock()

    def publish(
        self,
        invocation: ProviderProcessInvocation,
        event: InvocationProcessEvent,
    ) -> ProviderProcessPublication:
        if event.status == "started":
            return self._publish_start(invocation, event)
        return self._publish_exit(invocation, event)

    def _publish_start(
        self,
        invocation: ProviderProcessInvocation,
        event: InvocationProcessEvent,
    ) -> ProviderProcessPublication:
        state_path = self._state_path(invocation, event.attempt)
        state = self._started_state(invocation, event)
        publication = self._publish_state(state_path, state, require_absent=True)
        self._remember(publication.path, state)
        return publication

    def _started_state(
        self,
        invocation: ProviderProcessInvocation,
        event: InvocationProcessEvent,
    ) -> ProviderProcessState:
        identity = ProcessInspector().identity_for(event.pid)
        return ProviderProcessState(
            run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
            run_id=self._directories.run_id,
            run_key_name=self._directories.run_key_name,
            node_id=invocation.node_id,
            task_id=invocation.task_id,
            provider=invocation.provider,
            role=invocation.role,
            audit_round_num=invocation.audit_round_num,
            round_num=invocation.round_num,
            attempt=event.attempt,
            pid=event.pid,
            process_group_id=event.process_group_id,
            hostname=identity.hostname,
            process_start_identity=identity.start_identity,
            status="started",
            started_at=datetime.now().isoformat(),
        )

    def _publish_exit(
        self,
        invocation: ProviderProcessInvocation,
        event: InvocationProcessEvent,
    ) -> ProviderProcessPublication:
        state_path = self._state_path(invocation, event.attempt)
        expected_state = self._verified_state(state_path, invocation, event)
        exited_state = self._exited_state(expected_state, event)
        publication = self._publish_state(
            state_path,
            exited_state,
            require_absent=False,
        )
        self._remember(publication.path, exited_state)
        return publication

    def _verified_state(
        self,
        state_path: Path,
        invocation: ProviderProcessInvocation,
        event: InvocationProcessEvent,
    ) -> ProviderProcessState:
        current_state = self._read_state(state_path)
        self._validate_exit(current_state, invocation, event)
        expected_state = self._expected_state(state_path)
        if expected_state is None or current_state != expected_state:
            raise RuntimeError(
                "Provider process state changed unexpectedly before exit."
            )
        return expected_state

    @staticmethod
    def _exited_state(
        expected_state: ProviderProcessState,
        event: InvocationProcessEvent,
    ) -> ProviderProcessState:
        if event.returncode is None:
            raise RuntimeError("Provider process exit event is missing a return code.")
        exited_state = expected_state.model_copy(
            update={
                "status": "exited",
                "exited_at": datetime.now().isoformat(),
                "returncode": event.returncode,
            }
        )
        return ProviderProcessState.model_validate(exited_state.model_dump(mode="json"))

    def _remember(self, state_path: Path, state: ProviderProcessState) -> None:
        with self._lock:
            self._expected_states[state_path] = state

    def _expected_state(self, state_path: Path) -> ProviderProcessState | None:
        with self._lock:
            return self._expected_states.get(state_path)

    def _state_path(
        self,
        invocation: ProviderProcessInvocation,
        attempt: int,
    ) -> Path:
        filename = build_provider_process_state_filename(
            invocation.node_id,
            invocation.task_id,
            invocation.provider,
            invocation.role,
            invocation.audit_round_num,
            invocation.round_num,
            attempt,
        )
        return (
            ensure_contained_directory(
                self._directories.stages_dir,
                "manifests/provider-processes",
            )
            / filename
        )

    @staticmethod
    def _publish_state(
        path: Path,
        state: ProviderProcessState,
        require_absent: bool,
    ) -> ProviderProcessPublication:
        payload = json_bytes(state.model_dump(mode="json", exclude_none=True))
        writer = atomic_write_bytes_if_absent if require_absent else atomic_write_bytes
        published_path = writer(path, payload)
        return ProviderProcessPublication(
            path=published_path,
            signature=(len(payload), hashlib.sha256(payload).hexdigest()),
        )

    @staticmethod
    def _read_state(path: Path) -> ProviderProcessState:
        try:
            state = path.lstat()
            if not is_single_link_regular_file(state):
                raise RuntimeError("Provider process state is not a safe file.")
            return ProviderProcessState.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, RuntimeError, ValidationError) as exc:
            raise RuntimeError(
                "Provider process state is missing, malformed, or unreadable."
            ) from exc

    @staticmethod
    def _validate_exit(
        state: ProviderProcessState,
        invocation: ProviderProcessInvocation,
        event: InvocationProcessEvent,
    ) -> None:
        expected = (
            invocation.node_id,
            invocation.task_id,
            invocation.provider,
            invocation.role,
            invocation.audit_round_num,
            invocation.round_num,
            event.attempt,
            event.pid,
            event.process_group_id,
        )
        actual = (
            state.node_id,
            state.task_id,
            state.provider,
            state.role,
            state.audit_round_num,
            state.round_num,
            state.attempt,
            state.pid,
            state.process_group_id,
        )
        if actual != expected:
            raise RuntimeError("Provider process exit event does not match its state.")

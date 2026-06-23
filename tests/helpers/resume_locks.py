from __future__ import annotations

from pathlib import Path

from crewplane.artifacts.locks.process_identity import ProcessIdentity
from crewplane.core.execution_state import RunManifest


class FakeProcessInspector:
    def __init__(
        self,
        pid: int,
        start_identity: str,
        live: bool = False,
        live_checks: list[bool] | None = None,
    ) -> None:
        self.pid = pid
        self.start_identity = start_identity
        self.live = live
        self.live_checks = list(live_checks or [])

    def current(self) -> ProcessIdentity:
        return ProcessIdentity(
            pid=self.pid,
            hostname="host",
            start_identity=self.start_identity,
        )

    def is_live(self, identity: ProcessIdentity) -> bool:
        if identity.pid <= 0:
            return False
        if self.live_checks:
            return self.live_checks.pop(0)
        return self.live


class UnsafeProcessInspector(FakeProcessInspector):
    def is_live(self, identity: ProcessIdentity) -> bool:
        raise RuntimeError(f"unsupported process check for pid {identity.pid}")


def write_manifest_at_run_key(
    tmp_path: Path,
    run_key_name: str,
    manifest: RunManifest,
) -> Path:
    manifest_path = (
        tmp_path / "execution-stages" / run_key_name / "manifests" / "run.json"
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        manifest.model_dump_json(indent=2, exclude_none=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path

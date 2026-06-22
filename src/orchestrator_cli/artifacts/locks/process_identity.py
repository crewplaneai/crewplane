from __future__ import annotations

import os
import socket
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    hostname: str
    start_identity: str | None


class ProcessInspector:
    def current(self) -> ProcessIdentity:
        return ProcessIdentity(
            pid=os.getpid(),
            hostname=socket.gethostname(),
            start_identity=process_start_identity(os.getpid()),
        )

    def is_live(self, identity: ProcessIdentity) -> bool:
        if identity.hostname != socket.gethostname():
            raise RuntimeError("Cannot verify lock owner on a different host.")
        if not _pid_exists(identity.pid):
            return False
        current_start = process_start_identity(identity.pid)
        if current_start is None or identity.start_identity is None:
            raise RuntimeError(
                "Cannot safely verify lock owner process start identity."
            )
        return current_start == identity.start_identity


def process_start_identity(pid: int) -> str | None:
    stat_path = f"/proc/{pid}/stat"
    try:
        with open(stat_path, encoding="utf-8") as handle:
            fields = handle.read().split()
    except OSError:
        return None
    if len(fields) < 22:
        return None
    return fields[21]


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

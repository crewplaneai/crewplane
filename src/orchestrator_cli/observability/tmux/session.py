from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TmuxSessionIdentity:
    session_name: str
    socket_name: str
    window_target: str

    @classmethod
    def from_run(cls, run_id: str, socket_name: str) -> TmuxSessionIdentity:
        session_name = f"orchestrator-{run_id}"
        return cls(
            session_name=session_name,
            socket_name=socket_name,
            window_target=f"{session_name}:dashboard",
        )


@dataclass(frozen=True)
class TmuxSessionTargets:
    session_name: str
    socket_name: str
    window_target: str
    left_pane_id: str
    right_pane_id: str

    @classmethod
    def from_identity(
        cls,
        identity: TmuxSessionIdentity,
        left_pane_id: str,
        right_pane_id: str,
    ) -> TmuxSessionTargets:
        return cls(
            session_name=identity.session_name,
            socket_name=identity.socket_name,
            window_target=identity.window_target,
            left_pane_id=left_pane_id,
            right_pane_id=right_pane_id,
        )

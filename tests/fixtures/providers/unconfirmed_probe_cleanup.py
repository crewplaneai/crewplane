"""Run the CLI with failed cleanup confirmation after reaping real probe processes."""

from unittest.mock import patch

from crewplane.architecture.contracts import InvocationDiagnosticSink
from crewplane.cli.app import app
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
)
from crewplane.runtime.agent.process.posix_session import PosixProcessSession

original_drain_failed = PosixProcessSession.drain_failed


async def drain_with_unconfirmed_cleanup(
    session: PosixProcessSession, diagnostics: InvocationDiagnosticSink | None
) -> None:
    await original_drain_failed(session, diagnostics)
    assert session.process is not None
    raise ProcessDrainError(
        ProcessDrainEvidence(
            session.process.pid, session.process_group_id, True, False
        ),
        "Provider process group could not be drained within the finite TERM/KILL deadline.",
    )


if __name__ == "__main__":
    with patch.object(
        PosixProcessSession, "drain_failed", new=drain_with_unconfirmed_cleanup
    ):
        app()

"""Session collaborators for lifecycle failure injection without native processes."""

from unittest.mock import Mock

from crewplane.runtime.agent.process.windows_launch import WindowsLaunch


def windows_session_stub(launch: Mock | WindowsLaunch) -> WindowsLaunch:
    if isinstance(launch, WindowsLaunch):
        return launch
    session = WindowsLaunch()
    session.job = Mock()
    session.process = launch.process
    session.assigned = launch.assigned
    session.start = launch.start
    session.release = launch.release
    session.collect = launch.collect
    session.drain = launch.drain
    session.close = launch.close
    session.cleanup_error = launch.cleanup_error
    return session

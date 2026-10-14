"""Withhold cooperative Windows shutdown to exercise Job termination."""

import run_tests
import test_process


class UnresponsiveProcess(test_process.OwnedProcess):
    def signal_shutdown(self) -> None:
        pass


test_process.OwnedProcess = UnresponsiveProcess
raise SystemExit(run_tests.main())

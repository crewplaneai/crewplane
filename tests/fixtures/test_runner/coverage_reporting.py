import coverage

from crewplane import choose


def forbid_reporting_in_test_process(*args, **kwargs) -> None:
    del args, kwargs
    raise RuntimeError("coverage reporting reused the test process")


def test_choose() -> None:
    assert choose(True) == 1
    assert choose(False) == 2
    coverage.Coverage.report = forbid_reporting_in_test_process
    coverage.Coverage.json_report = forbid_reporting_in_test_process

import pytest
from typer.testing import CliRunner

from crewplane.cli import app as cli


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_discovery_timeout_rejects_nonpositive_or_nonfinite_values(value):
    result = CliRunner().invoke(
        cli.app, ["onboarding", "--discovery-timeout", value], env={"COLUMNS": "160"}
    )
    assert result.exit_code == 2
    assert "finite number greater than 0" in result.output


@pytest.mark.parametrize(
    "options,expected", [([], 15.0), (["--discovery-timeout", "60"], 60.0)]
)
def test_discovery_timeout_reaches_onboarding(monkeypatch, options, expected):
    received = []
    monkeypatch.setattr(cli, "run_onboarding_command", received.append)
    result = CliRunner().invoke(cli.app, ["onboarding", *options])
    assert result.exit_code == 0, result.output
    assert received == [expected]

from orchestrator_cli.runtime.agent.retry_units import (
    format_stream_retry_wait,
    format_wait_duration,
    normalize_retry_wait_units_in_text,
)


def test_format_wait_duration():
    assert format_wait_duration(0.5) == "0.500s"
    assert format_wait_duration(5) == "5s"
    assert format_wait_duration(65) == "1m5s"
    assert format_wait_duration(3605) == "1h5s"
    assert format_wait_duration(90065) == "1d1h1m5s"


def test_format_stream_retry_wait():
    assert format_stream_retry_wait(0.5) == "0.5s"
    assert format_stream_retry_wait(5.0) == "5s"
    assert format_stream_retry_wait(10.5) == "10s"
    assert format_stream_retry_wait(65.0) == "1m5s"


def test_normalize_retry_wait_units_in_text():
    text = "Retrying after 1500ms"
    assert normalize_retry_wait_units_in_text(text) == "Retrying after 1.5s"

    text = "Retrying after 65000ms"
    assert normalize_retry_wait_units_in_text(text) == "Retrying after 1m5s"

    # Edge cases
    assert (
        normalize_retry_wait_units_in_text("Retrying after 500.5ms")
        == "Retrying after 0.5s"
    )
    assert (
        normalize_retry_wait_units_in_text("Retrying after invalidms")
        == "Retrying after invalidms"
    )

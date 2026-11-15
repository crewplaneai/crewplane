import pytest
from pydantic import ValidationError

from crewplane.core.config import Config, MemorySettings, require_memory_extractor
from crewplane.version import SCHEMA_VERSION


def test_memory_policy_defaults_preserve_disabled_config_loading() -> None:
    config = Config(version=SCHEMA_VERSION, agents={})
    assert config.settings.memory == MemorySettings(
        learn=True,
        extractor=None,
        max_records=5,
        max_chars=8000,
        extraction_timeout_seconds=120.0,
    )
    with pytest.raises(ValueError, match="extractor"):
        require_memory_extractor(config)


@pytest.mark.parametrize("field", ["max_records", "max_chars"])
@pytest.mark.parametrize("value", [None, True, False, "5", 1.0, 0, -1])
def test_memory_integer_limits_are_strict_positive(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=field):
        MemorySettings.model_validate({field: value})


@pytest.mark.parametrize("value", [None, "true", "false", 0, 1])
def test_memory_learning_flag_requires_boolean(value: object) -> None:
    with pytest.raises(ValidationError, match="learn"):
        MemorySettings.model_validate({"learn": value})


@pytest.mark.parametrize(
    "value", [None, True, False, "120", 0, -1, float("inf"), float("nan")]
)
def test_memory_timeout_requires_positive_finite_number(value: object) -> None:
    with pytest.raises(ValidationError, match="extraction_timeout_seconds"):
        MemorySettings.model_validate({"extraction_timeout_seconds": value})


def test_memory_extractor_is_validated_only_when_requested() -> None:
    config = Config.model_validate(
        {
            "version": SCHEMA_VERSION,
            "agents": {"alpha": {"cli_cmd": ["echo"], "default_model": "chosen"}},
            "settings": {"memory": {"learn": False, "extractor": "missing"}},
        }
    )
    with pytest.raises(ValueError, match="missing.*configured"):
        require_memory_extractor(config)
    config.settings.memory.extractor = "alpha"
    assert require_memory_extractor(config) == "alpha"
    assert config.agents["alpha"].default_model == "chosen"


@pytest.mark.parametrize("value", ["", " ", True, 1, []])
def test_memory_extractor_rejects_invalid_names(value: object) -> None:
    with pytest.raises(ValidationError, match="extractor"):
        MemorySettings.model_validate({"extractor": value})


def test_memory_policy_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        MemorySettings.model_validate({"automatic_accept": True})

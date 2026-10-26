from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, ValidationError
from yaml import YAMLError

from crewplane.core.yaml_loader import load_yaml_unique


class OllamaSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    readiness_timeout_seconds: FiniteFloat = Field(default=900.0, gt=0)


def load_settings(project_root: Path) -> OllamaSettings:
    path = project_root / ".crewplane" / "ollama.yml"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return OllamaSettings()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Cannot read Ollama settings in {path}: {exc}") from exc
    try:
        return OllamaSettings.model_validate(load_yaml_unique(text))
    except (ValidationError, YAMLError) as exc:
        raise ValueError(f"Invalid Ollama settings in {path}: {exc}") from exc

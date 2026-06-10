from pathlib import Path
from typing import Final, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
from yaml.constructor import ConstructorError

from orchestrator_cli.architecture.contracts import (
    JsonObject,
    PromptTransport,
    ProviderKind,
)
from orchestrator_cli.version import SCHEMA_VERSION

from .provider_names import normalize_provider_name
from .token_budget import TokenBudgetSettings
from .workflow_keywords import (
    ALLOWED_SEQUENTIAL_CONSENSUS_POLICIES,
    ALLOWED_SEQUENTIAL_CONSENSUS_POLICY_SET,
    SequentialConsensusPolicy,
    validate_exact_keyword,
)
from .yaml_loader import load_yaml_unique

ALLOWED_PROVIDER_KINDS: Final = (
    "claude",
    "codex",
    "copilot",
    "gemini",
    "kilo",
    "generic",
)
ALLOWED_PROVIDER_KIND_SET: Final = set(ALLOWED_PROVIDER_KINDS)
ALLOWED_PROMPT_TRANSPORTS: Final = ("stdin", "argv")
ALLOWED_PROMPT_TRANSPORT_SET: Final = set(ALLOWED_PROMPT_TRANSPORTS)
DEFAULT_MOCK_INVOKER_OBSERVATION_DELAY_SECONDS: Final = 5.0

# Default policy: do not wall-clock kill active provider CLIs. The idle timeout
# remains the default safety guard for quiet/stalled processes; set this value
# only when the deployment needs an absolute per-attempt cap.
DEFAULT_INVOCATION_TIMEOUT_SECONDS: Final[float | None] = None
DEFAULT_INVOCATION_IDLE_TIMEOUT_SECONDS: Final = 1800.0


def _validate_command_token(value: str | None, field_name: str) -> str | None:
    if value is not None and not value.strip():
        raise ValueError(f"{field_name} cannot be blank")
    return value


class TokenPricing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: float | None = Field(default=None, ge=0)
    cached_input: float | None = Field(default=None, ge=0)
    cache_write: float | None = Field(default=None, ge=0)
    output: float | None = Field(default=None, ge=0)
    reasoning: float | None = Field(default=None, ge=0)
    total: float | None = Field(default=None, ge=0)

    @field_validator("total")
    @classmethod
    def _validate_total_exclusivity(
        cls, value: float | None, info: ValidationInfo
    ) -> float | None:
        if value is None:
            return value
        sibling_keys = ("input", "cached_input", "cache_write", "output", "reasoning")
        configured_siblings = [
            key for key in sibling_keys if info.data.get(key) is not None
        ]
        if configured_siblings:
            joined = ", ".join(configured_siblings)
            raise ValueError(
                "pricing.total cannot be combined with bucket-specific pricing: "
                f"{joined}"
            )
        return value

    def configured_buckets(self) -> tuple[str, ...]:
        return tuple(
            bucket
            for bucket in (
                "input",
                "cached_input",
                "cache_write",
                "output",
                "reasoning",
                "total",
            )
            if getattr(self, bucket) is not None
        )

    def as_dict(self) -> dict[str, float | None]:
        return {
            "input": self.input,
            "cached_input": self.cached_input,
            "cache_write": self.cache_write,
            "output": self.output,
            "reasoning": self.reasoning,
            "total": self.total,
        }


class AgentConfig(BaseModel):
    """Per-provider CLI invocation settings."""

    model_config = ConfigDict(extra="forbid")

    cli_cmd: list[str]
    provider_kind: ProviderKind = "generic"
    default_model: str | None = None

    model_arg: str | None = "--model"
    prompt_transport: PromptTransport = "stdin"
    prompt_transport_arg: str | None = None
    extra_args: list[str] = Field(default_factory=list)
    max_retries: int = Field(default=0, ge=0)
    retry_delay_seconds: float = Field(default=300.0, ge=0)
    retry_on_exit_codes: list[int] = Field(default_factory=list)
    retry_on_stderr_contains: list[str] = Field(default_factory=list)
    retry_on_output_contains: list[str] = Field(default_factory=list)
    quota_reached_on_contains: list[str] = Field(default_factory=list)
    quota_reached_retry_delay_seconds: float = Field(default=300.0, ge=0)
    quota_reset_sleep_floor_seconds: float = Field(default=5.0, ge=0)
    # A finite wall-clock timeout cancels and reaps the provider process.
    invocation_timeout_seconds: float | None = DEFAULT_INVOCATION_TIMEOUT_SECONDS
    invocation_idle_timeout_seconds: float | None = (
        DEFAULT_INVOCATION_IDLE_TIMEOUT_SECONDS
    )
    pricing: "TokenPricing" = Field(default_factory=lambda: TokenPricing())

    @field_validator("cli_cmd")
    @classmethod
    def _validate_cli_cmd(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("cli_cmd must contain at least one token")
        if any(not token.strip() for token in value):
            raise ValueError("cli_cmd cannot contain blank tokens")
        return value

    @field_validator("provider_kind", mode="before")
    @classmethod
    def _validate_provider_kind(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower()
        if normalized not in ALLOWED_PROVIDER_KIND_SET:
            raise ValueError(
                f"provider_kind must be one of: {', '.join(ALLOWED_PROVIDER_KINDS)}"
            )
        return normalized

    @field_validator("prompt_transport", mode="before")
    @classmethod
    def _validate_prompt_transport(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower()
        if normalized not in ALLOWED_PROMPT_TRANSPORT_SET:
            raise ValueError(
                "prompt_transport must be one of: "
                f"{', '.join(ALLOWED_PROMPT_TRANSPORTS)}"
            )
        return normalized

    @field_validator("model_arg", "prompt_transport_arg")
    @classmethod
    def _validate_optional_command_token(cls, value: str | None) -> str | None:
        return _validate_command_token(value, "command argument")

    @model_validator(mode="after")
    def _validate_prompt_transport_arg(self) -> Self:
        if self.prompt_transport == "argv" and self.prompt_transport_arg is None:
            raise ValueError(
                "prompt_transport_arg is required when prompt_transport is argv"
            )
        if self.prompt_transport == "stdin" and self.prompt_transport_arg == "":
            raise ValueError("prompt_transport_arg cannot be blank")
        return self

    @field_validator("extra_args")
    @classmethod
    def _validate_extra_args(cls, value: list[str]) -> list[str]:
        if any(not token.strip() for token in value):
            raise ValueError("extra_args cannot contain blank tokens")
        return value

    @field_validator("retry_on_exit_codes")
    @classmethod
    def _validate_exit_codes(cls, value: list[int]) -> list[int]:
        for code in value:
            if not 0 <= code <= 255:
                raise ValueError("exit codes must be between 0 and 255")
        return value

    @field_validator(
        "invocation_timeout_seconds",
        "invocation_idle_timeout_seconds",
    )
    @classmethod
    def _validate_invocation_timeout(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("invocation timeout values must be greater than 0")
        return value

    def get_command(self) -> list[str]:
        return list(self.cli_cmd)


class IntegrationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    implementation: str
    options: JsonObject = Field(default_factory=dict)

    @field_validator("implementation", mode="before")
    @classmethod
    def _validate_implementation(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("implementation must be a non-empty string")
        return normalized


class IntegrationsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoker: IntegrationSpec = Field(
        default_factory=lambda: IntegrationSpec(implementation="cli")
    )
    ui: IntegrationSpec = Field(
        default_factory=lambda: IntegrationSpec(
            implementation="tmux",
            options={
                "auto_close_session": True,
                "quiet_after_seconds": 120.0,
            },
        )
    )
    artifacts: IntegrationSpec = Field(
        default_factory=lambda: IntegrationSpec(
            implementation="filesystem",
            options={
                "log_cli_output": True,
                "allowed_template_paths": [],
            },
        )
    )


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_workspace: str = ".orchestrator/workspaces"
    log_level: str = "info"
    sequential_consensus_on_exhaustion: SequentialConsensusPolicy = "continue"
    max_audit_rounds: int = Field(default=5, ge=1)
    max_concurrent_nodes: int | None = Field(default=None, ge=1)
    max_parallel_invocations: int | None = Field(default=None, ge=1)
    token_budget: TokenBudgetSettings = Field(default_factory=TokenBudgetSettings)
    integrations: IntegrationsConfig = Field(default_factory=IntegrationsConfig)

    @field_validator("sequential_consensus_on_exhaustion", mode="before")
    @classmethod
    def _validate_sequential_consensus_on_exhaustion(cls, value: object) -> object:
        return validate_exact_keyword(
            value,
            field_name="sequential_consensus_on_exhaustion",
            allowed_values=ALLOWED_SEQUENTIAL_CONSENSUS_POLICIES,
            allowed_value_set=ALLOWED_SEQUENTIAL_CONSENSUS_POLICY_SET,
        )


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    agents: dict[str, AgentConfig]
    settings: Settings | None = None

    @field_validator("agents", mode="before")
    @classmethod
    def _validate_agent_names(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized_agents: dict[str, object] = {}
        for raw_name, agent_config in value.items():
            if not isinstance(raw_name, str):
                raise ValueError("agent names must be strings")
            normalized_name = normalize_provider_name(raw_name, "agent name")
            if not isinstance(normalized_name, str):
                raise ValueError("agent names must be strings")
            if normalized_name in normalized_agents:
                raise ValueError(
                    f"Duplicate agent name after trimming: '{normalized_name}'"
                )
            normalized_agents[normalized_name] = agent_config
        return normalized_agents

    @field_validator("version")
    @classmethod
    def _validate_config_version(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported config version '{value}'. "
                f"Expected '{SCHEMA_VERSION}'. "
                "Run 'orchestrator init' to regenerate config files."
            )
        return value


def load_config(path: Path) -> Config:
    """Load a config file from YAML."""

    try:
        data = load_yaml_unique(path.read_text(encoding="utf-8"))
    except ConstructorError as error:
        raise ValueError(f"{path} is invalid: {error}") from error

    if not isinstance(data, dict):
        raise ValueError("Config file must contain a YAML object.")

    return Config(**data)

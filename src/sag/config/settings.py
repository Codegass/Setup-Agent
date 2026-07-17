"""Configuration settings for Setup-Agent (SAG)."""

import os
from pathlib import Path
from typing import Dict, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from .models import LogLevel

# Test verdict policy: the required fraction of executed tests that must pass for a
# build-green run to count as a SUCCESS. This is the SINGLE SOURCE OF TRUTH for the
# pass-rate gate consumed by both the report verdict and the run/test success policy
# (replaces the previously hardcoded "80%" magic number scattered across modules).
DEFAULT_TEST_PASS_THRESHOLD = 0.8

# Build verdict policy: the required fraction of EXPECTED compiled classes (source-
# weighted across the ACTIVE reactor modules) that must actually be produced for a
# build to count as a full SUCCESS. A setup is only a success when every active
# module compiles, so the default is 1.0 ("every active module must build"). Modules
# disabled in the build config (profile-gated, commented out, not in the effective
# reactor) are never counted as expected. Still configurable via
# SAG_BUILD_COVERAGE_THRESHOLD to loosen per-run when needed; a partial build (real
# output but below this threshold) is reported as PARTIAL, never SUCCESS.
DEFAULT_BUILD_COVERAGE_THRESHOLD = 1.0

# Test verdict policy: the required fraction of DETECTED tests that must actually be
# EXECUTED for a build-green run to count as a full SUCCESS. Mirrors the build
# coverage gate but for test execution: a run that detected a static suite (e.g.
# 1122 tests) yet only ran a fraction of it (e.g. 1) is reported as PARTIAL, never
# SUCCESS — the test suite was not really exercised. Configurable via
# SAG_TEST_EXECUTION_THRESHOLD; 0.8 = "most detected tests must run".
DEFAULT_TEST_EXECUTION_THRESHOLD = 0.8


class Config(BaseModel):
    """Main configuration class."""

    # Model configuration - separate thinking and action models
    thinking_model: str = Field(default="gpt-5.4-mini")
    thinking_provider: str = Field(default="openai")
    thinking_temperature: float = Field(default=0.1)
    thinking_max_tokens: int = Field(default=16000)
    reasoning_effort: str = Field(default="medium")  # for o1 models and claude thinking
    thinking_budget_tokens: int = Field(default=10000)  # for claude thinking budget

    # GPT-5 specific parameters
    verbosity: str = Field(default="medium")  # for GPT-5 models
    gpt5_reasoning_effort: str = Field(default="medium")  # for GPT-5 models

    action_model: str = Field(default="gpt-4o")
    action_provider: str = Field(default="openai")
    action_temperature: float = Field(default=0.3)
    action_max_tokens: int = Field(default=2000)

    # API Keys (will be loaded from environment)
    openai_api_key: Optional[str] = Field(default=None)
    anthropic_api_key: Optional[str] = Field(default=None)
    groq_api_key: Optional[str] = Field(default=None)
    azure_api_key: Optional[str] = Field(default=None)

    # Base URLs
    openai_base_url: str = Field(default="https://api.openai.com/v1")
    ollama_base_url: str = Field(default="http://localhost:11434")
    azure_api_base: Optional[str] = Field(default=None)
    azure_api_version: str = Field(default="2023-12-01-preview")

    # Logging configuration
    log_level: LogLevel = Field(default=LogLevel.INFO)
    log_file: Optional[str] = Field(default="logs/sag.log")
    verbose: bool = Field(default=False)  # Enable verbose debugging output
    ui_mode: bool = Field(default=False)  # Enable enhanced UI mode with live progress display
    log_rotation: str = Field(default="50 MB")  # Log file rotation size
    log_retention: str = Field(default="30 days")  # Log file retention period

    # Docker configuration
    docker_base_image: str = Field(default="ubuntu:24.04")
    workspace_path: str = Field(default="/workspace")

    # Agent configuration
    max_iterations: int = Field(default=50)
    context_switch_threshold: int = Field(default=20)
    # Experimental scheduler heartbeat: require a fresh reasoning turn after
    # this many actor calls without one.  This is a tunable guard, not part of
    # the executable-plan architecture.
    reasoning_heartbeat_actions: int = Field(default=5, ge=1)
    # Global wall-clock cap for a whole run (seconds); the ReAct loop ends with
    # a clear "global time cap" status once exceeded. <=0 disables the cap.
    max_wall_clock_seconds: int = Field(default=7200)

    # Minimum iterations RESERVED for each not-yet-started phase. No phase has
    # a quota (build may use everything the easy phases saved); the engine
    # force-blocks the current phase only when continuing would starve these
    # floors — guaranteeing the run always reaches report and ends honestly.
    phase_min_floors: Dict[str, int] = Field(
        default_factory=lambda: {"analyze": 4, "build": 10, "test": 12, "report": 8}
    )

    # Dispatch-and-poll execution for long build/test commands: the command
    # runs detached (output to a container log file). If still running when the
    # soft window closes, the tool hands back the log tail + poll instructions
    # instead of killing the process.
    dispatch_soft_timeout_seconds: int = Field(default=900)
    dispatch_poll_interval_seconds: int = Field(default=15)

    # Validation / verdict policy
    # Minimum test pass rate (fraction, 0-1) for a build-green run to be a SUCCESS.
    test_pass_threshold: float = Field(default=DEFAULT_TEST_PASS_THRESHOLD)
    # Minimum source-weighted compiled-class coverage (fraction, 0-1) for a
    # multi-module build to count as green.
    build_coverage_threshold: float = Field(default=DEFAULT_BUILD_COVERAGE_THRESHOLD)
    # Minimum fraction (0-1) of DETECTED tests that must be executed for a
    # build-green run to be a SUCCESS (else the run is capped at PARTIAL).
    test_execution_threshold: float = Field(default=DEFAULT_TEST_EXECUTION_THRESHOLD)

    @classmethod
    def from_env(cls) -> "Config":
        """Create configuration from environment variables."""
        # Load .env file if it exists
        env_file = Path(".env")
        if env_file.exists():
            load_dotenv(env_file)

        return cls(
            # Thinking model config
            thinking_model=os.getenv("SAG_THINKING_MODEL", "gpt-5.4-mini"),
            thinking_provider=os.getenv("SAG_THINKING_PROVIDER", "openai"),
            thinking_temperature=float(os.getenv("SAG_THINKING_TEMPERATURE", "0.1")),
            thinking_max_tokens=int(os.getenv("SAG_MAX_THINKING_TOKENS", "16000")),
            reasoning_effort=os.getenv("SAG_REASONING_EFFORT", "medium"),
            thinking_budget_tokens=int(os.getenv("SAG_THINKING_BUDGET_TOKENS", "10000")),
            # GPT-5 specific parameters
            verbosity=os.getenv("SAG_VERBOSITY", "medium"),
            gpt5_reasoning_effort=os.getenv("SAG_GPT5_REASONING_EFFORT", "medium"),
            # Action model config
            action_model=os.getenv("SAG_ACTION_MODEL", "gpt-4o"),
            action_provider=os.getenv("SAG_ACTION_PROVIDER", "openai"),
            action_temperature=float(os.getenv("SAG_ACTION_TEMPERATURE", "0.3")),
            action_max_tokens=int(os.getenv("SAG_MAX_ACTION_TOKENS", "10000")),
            # API Keys
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            groq_api_key=os.getenv("GROQ_API_KEY"),
            azure_api_key=os.getenv("AZURE_API_KEY"),
            # Base URLs
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            azure_api_base=os.getenv("AZURE_API_BASE"),
            azure_api_version=os.getenv("AZURE_API_VERSION", "2023-12-01-preview"),
            # System config
            log_level=LogLevel(os.getenv("SAG_LOG_LEVEL", "INFO")),
            log_file=os.getenv("SAG_LOG_FILE", "logs/sag.log"),
            verbose=os.getenv("SAG_VERBOSE", "false").lower() in ("true", "1", "yes"),
            ui_mode=os.getenv("SAG_UI_MODE", "false").lower() in ("true", "1", "yes"),
            log_rotation=os.getenv("SAG_LOG_ROTATION", "50 MB"),
            log_retention=os.getenv("SAG_LOG_RETENTION", "30 days"),
            docker_base_image=os.getenv("SAG_DOCKER_BASE_IMAGE", "ubuntu:24.04"),
            workspace_path=os.getenv("SAG_WORKSPACE_PATH", "/workspace"),
            max_iterations=int(os.getenv("SAG_MAX_ITERATIONS", "50")),
            context_switch_threshold=int(os.getenv("SAG_CONTEXT_SWITCH_THRESHOLD", "20")),
            reasoning_heartbeat_actions=int(os.getenv("SAG_REASONING_HEARTBEAT_ACTIONS", "5")),
            max_wall_clock_seconds=int(os.getenv("SAG_MAX_WALL_CLOCK_SECONDS", "7200")),
            dispatch_soft_timeout_seconds=int(
                os.getenv("SAG_DISPATCH_SOFT_TIMEOUT_SECONDS", "900")
            ),
            dispatch_poll_interval_seconds=int(
                os.getenv("SAG_DISPATCH_POLL_INTERVAL_SECONDS", "15")
            ),
            test_pass_threshold=float(
                os.getenv("SAG_TEST_PASS_THRESHOLD", str(DEFAULT_TEST_PASS_THRESHOLD))
            ),
            build_coverage_threshold=float(
                os.getenv("SAG_BUILD_COVERAGE_THRESHOLD", str(DEFAULT_BUILD_COVERAGE_THRESHOLD))
            ),
            test_execution_threshold=float(
                os.getenv("SAG_TEST_EXECUTION_THRESHOLD", str(DEFAULT_TEST_EXECUTION_THRESHOLD))
            ),
        )

    def get_litellm_model_name(self, model_type: str = "action") -> str:
        """Get the full model name for LiteLLM."""
        if model_type == "thinking":
            provider = self.thinking_provider
            model = self.thinking_model
        else:
            provider = self.action_provider
            model = self.action_model

        # LiteLLM format: provider/model
        if provider == "openai":
            return model  # OpenAI models don't need prefix
        elif provider == "anthropic":
            return f"anthropic/{model}"
        elif provider == "groq":
            return f"groq/{model}"
        elif provider == "ollama":
            return f"ollama/{model}"
        elif provider == "azure":
            return f"azure/{model}"
        else:
            return f"{provider}/{model}"

    def is_gpt5_model(self, model_type: str = "action") -> bool:
        """Check if the specified model is a GPT-5 variant."""
        if model_type == "thinking":
            model = self.thinking_model.lower()
        else:
            model = self.action_model.lower()

        # Only check for actual GPT-5 variants (not GPT-4.1)
        return "gpt5" in model or "gpt-5" in model

    def get_thinking_config(self) -> dict:
        """Get thinking configuration based on provider."""
        if self.thinking_provider == "anthropic":
            if self._uses_anthropic_adaptive_thinking():
                return {"reasoning_effort": self.reasoning_effort}

            effort_mapping = {"low": 1024, "medium": 2048, "high": 4096}
            budget_tokens = effort_mapping.get(self.reasoning_effort, self.thinking_budget_tokens)
            return {"thinking": {"type": "enabled", "budget_tokens": budget_tokens}}
        elif self.thinking_provider == "openai" and "o1" in self.thinking_model:
            # For OpenAI o1 models, use reasoning_effort
            return {"reasoning_effort": self.reasoning_effort}
        elif self.thinking_provider == "openai" and self.is_gpt5_model("thinking"):
            # For OpenAI GPT-5 models, use reasoning_effort only
            return {"reasoning_effort": self.gpt5_reasoning_effort}
        else:
            # For other models, no special thinking config
            return {}

    def _uses_anthropic_adaptive_thinking(self) -> bool:
        """Return True for Anthropic models where LiteLLM maps reasoning_effort."""
        model = self.thinking_model.lower()
        adaptive_markers = (
            "4-6",
            "4.6",
            "4-7",
            "4.7",
            "opus-4-5",
            "opus-4.5",
        )
        return any(marker in model for marker in adaptive_markers)


def effective_phase_floor(floor: int, max_iterations: int) -> int:
    """Scale a floor down for small runs so floors can never exceed the cap.

    Normal-sized runs keep their configured floors verbatim; only tiny runs
    (where the summed floors would eat the whole iteration budget) clamp each
    floor to a fraction of the cap.
    """
    return max(1, min(int(floor), max(2, max_iterations // 10)))


def setup_litellm_environment(config: Config) -> None:
    """Setup environment variables for LiteLLM."""

    # Set API keys for LiteLLM
    if config.openai_api_key:
        os.environ["OPENAI_API_KEY"] = config.openai_api_key
    if config.anthropic_api_key:
        os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key
    if config.groq_api_key:
        os.environ["GROQ_API_KEY"] = config.groq_api_key
    if config.azure_api_key:
        os.environ["AZURE_API_KEY"] = config.azure_api_key

    # Set base URLs
    if config.openai_base_url:
        os.environ["OPENAI_API_BASE"] = config.openai_base_url
    if config.azure_api_base:
        os.environ["AZURE_API_BASE"] = config.azure_api_base
        os.environ["AZURE_API_VERSION"] = config.azure_api_version

    # LiteLLM environment configured

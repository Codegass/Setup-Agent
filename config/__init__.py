"""Configuration module for Setup-Agent (SAG)."""

import os
import sys
from enum import Enum
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field


class LogLevel(str, Enum):
    """Log levels."""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class Config(BaseModel):
    """Main configuration class."""
    
    # Model configuration - separate thinking and action models
    thinking_model: str = Field(default="o1-preview")
    thinking_provider: str = Field(default="openai")
    thinking_temperature: float = Field(default=0.1)
    thinking_max_tokens: int = Field(default=8000)
    reasoning_effort: str = Field(default="medium")  # for o1 models
    
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
    log_rotation: str = Field(default="50 MB")  # Log file rotation size
    log_retention: str = Field(default="30 days")  # Log file retention period
    
    # Docker configuration
    docker_base_image: str = Field(default="ubuntu:22.04")
    workspace_path: str = Field(default="/workspace")
    
    # Agent configuration
    max_iterations: int = Field(default=50)
    context_switch_threshold: int = Field(default=20)
    
    @classmethod
    def from_env(cls) -> "Config":
        """Create configuration from environment variables."""
        # Load .env file if it exists
        env_file = Path(".env")
        if env_file.exists():
            load_dotenv(env_file)
        
        return cls(
            # Thinking model config
            thinking_model=os.getenv("SAG_THINKING_MODEL", "o4-mini"),
            thinking_provider=os.getenv("SAG_THINKING_PROVIDER", "openai"),
            thinking_temperature=float(os.getenv("SAG_THINKING_TEMPERATURE", "0.1")),
            thinking_max_tokens=int(os.getenv("SAG_MAX_THINKING_TOKENS", "10000")),
            reasoning_effort=os.getenv("SAG_REASONING_EFFORT", "medium"),
            
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
            log_rotation=os.getenv("SAG_LOG_ROTATION", "50 MB"),
            log_retention=os.getenv("SAG_LOG_RETENTION", "30 days"),
            docker_base_image=os.getenv("SAG_DOCKER_BASE_IMAGE", "ubuntu:22.04"),
            workspace_path=os.getenv("SAG_WORKSPACE_PATH", "/workspace"),
            max_iterations=int(os.getenv("SAG_MAX_ITERATIONS", "50")),
            context_switch_threshold=int(os.getenv("SAG_CONTEXT_SWITCH_THRESHOLD", "20")),
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


def setup_logging(config: Config):
    """Setup logging configuration using the new session-based system."""
    from .logger import setup_session_logging
    return setup_session_logging(config)


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


# Global configuration instance
_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = Config.from_env()
        setup_logging(_config)
        setup_litellm_environment(_config)
    return _config


def set_config(config: Config) -> None:
    """Set the global configuration instance."""
    global _config
    _config = config
    setup_logging(config)
    setup_litellm_environment(config)


def create_agent_logger(context_id: str):
    """Create a specialized logger for agent execution traces."""
    from .logger import create_agent_logger as _create_agent_logger
    return _create_agent_logger(context_id)

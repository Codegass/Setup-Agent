"""Configuration module for Setup-Agent (SAG)."""

from typing import Optional

from .logger import setup_session_logging, create_agent_logger, create_command_logger, get_session_logger, create_verbose_logger
from .models import LogLevel
from .settings import Config, setup_litellm_environment


def setup_logging(config: Config):
    """Setup logging configuration using the new session-based system."""
    return setup_session_logging(config)


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


# Convenience exports
__all__ = [
    "Config",
    "LogLevel", 
    "get_config",
    "set_config",
    "setup_logging",
    "setup_litellm_environment",
    "create_agent_logger",
    "create_command_logger",
    "get_session_logger",
    "create_verbose_logger",
]

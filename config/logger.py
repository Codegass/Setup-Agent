"""Advanced logging system for SAG with session-based and verbose controls."""

import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger


class SessionLogger:
    """Manages session-specific logging with timestamp-based separation."""
    
    def __init__(self, config):
        self.config = config
        self.session_id = self._generate_session_id()
        self.base_log_dir = Path("logs")
        self.session_log_dir = self.base_log_dir / f"session_{self.session_id}"
        
        # Create log directories
        self.base_log_dir.mkdir(exist_ok=True)
        self.session_log_dir.mkdir(exist_ok=True)
        
        # Initialize loggers
        self._setup_loggers()
        
        logger.info(f"Session logging initialized. Session ID: {self.session_id}")
        logger.info(f"Session logs directory: {self.session_log_dir}")
    
    def _generate_session_id(self) -> str:
        """Generate a unique session ID based on timestamp."""
        return datetime.now().strftime("%Y%m%d_%H%M%S")
    
    def _setup_loggers(self):
        """Setup all loggers with session-specific configuration."""
        
        # Remove default logger
        logger.remove()
        
        # Console logger - respects verbose setting
        console_level = "DEBUG" if self.config.verbose else self.config.log_level.value
        logger.add(
            sys.stderr,
            level=console_level,
            format=self._get_console_format(),
            colorize=True,
            filter=self._console_filter
        )
        
        # Main session log file - always captures everything
        main_log_file = self.session_log_dir / "main.log"
        logger.add(
            str(main_log_file),
            level="DEBUG",
            format=self._get_file_format(),
            rotation=self.config.log_rotation,
            retention=self.config.log_retention,
            compression="gz",
            enqueue=True,
        )
        
        # Agent execution log - specialized for agent traces
        agent_log_file = self.session_log_dir / "agent_execution.log"
        logger.add(
            str(agent_log_file),
            level="INFO",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {extra[context_id]} | {message}",
            filter=lambda record: "AGENT_TRACE" in record.get("extra", {}),
            rotation="100 MB",
            retention="7 days",
        )
        
        # Error log - only errors and critical
        error_log_file = self.session_log_dir / "errors.log"
        logger.add(
            str(error_log_file),
            level="ERROR",
            format=self._get_file_format(),
            rotation="10 MB",
            retention="90 days",
        )
        
        # Verbose debug log - only when verbose is enabled
        if self.config.verbose:
            debug_log_file = self.session_log_dir / "debug_verbose.log"
            logger.add(
                str(debug_log_file),
                level="TRACE",  # Captures everything including LiteLLM internals
                format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
                rotation="200 MB",
                retention="3 days",
                filter=lambda record: "VERBOSE" in record.get("extra", {}) or record["level"].name in ["TRACE", "DEBUG"]
            )
        
        # Legacy main log file (for backward compatibility)
        if self.config.log_file:
            legacy_log_path = Path(self.config.log_file)
            legacy_log_path.parent.mkdir(parents=True, exist_ok=True)
            logger.add(
                str(legacy_log_path),
                level="INFO",
                format=self._get_file_format(),
                rotation=self.config.log_rotation,
                retention=self.config.log_retention,
                compression="gz",
            )
    
    def _get_console_format(self) -> str:
        """Get console log format based on verbose setting."""
        if self.config.verbose:
            return ("<green>{time:HH:mm:ss.SSS}</green> | "
                   "<level>{level: <8}</level> | "
                   "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                   "<level>{message}</level>")
        else:
            return ("<green>{time:HH:mm:ss}</green> | "
                   "<level>{level: <8}</level> | "
                   "<level>{message}</level>")
    
    def _get_file_format(self) -> str:
        """Get file log format."""
        return "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}"
    
    def _console_filter(self, record):
        """Filter console output based on verbose setting."""
        # Always show INFO and above
        if record["level"].no >= 20:  # INFO level
            return True
        
        # Only show DEBUG and TRACE in verbose mode
        if self.config.verbose and record["level"].no >= 10:  # DEBUG level
            return True
        
        return False
    
    def create_agent_logger(self, context_id: str):
        """Create a specialized logger for agent execution traces."""
        return logger.bind(AGENT_TRACE=True, context_id=context_id)
    
    def create_verbose_logger(self, name: str):
        """Create a logger that outputs to verbose debug log."""
        return logger.bind(VERBOSE=True, logger_name=name)
    
    def create_command_logger(self, command: str, project_name: str):
        """Create a logger for a specific command execution."""
        command_log_file = self.session_log_dir / f"command_{command}_{project_name}.log"
        
        # Add a command-specific logger
        command_logger_id = logger.add(
            str(command_log_file),
            level="DEBUG",
            format=self._get_file_format(),
            filter=lambda record: record.get("extra", {}).get("command") == command,
            rotation="50 MB",
            retention="7 days",
        )
        
        return logger.bind(command=command, project_name=project_name), command_logger_id
    
    def cleanup_command_logger(self, logger_id):
        """Remove a command-specific logger."""
        try:
            logger.remove(logger_id)
        except Exception as e:
            logger.warning(f"Failed to cleanup command logger: {e}")
    
    def get_session_summary(self) -> dict:
        """Get a summary of the current logging session."""
        log_files = list(self.session_log_dir.glob("*.log"))
        
        summary = {
            "session_id": self.session_id,
            "session_dir": str(self.session_log_dir),
            "verbose_enabled": self.config.verbose,
            "log_level": self.config.log_level.value,
            "log_files": [
                {
                    "name": f.name,
                    "size": f.stat().st_size if f.exists() else 0,
                    "path": str(f)
                }
                for f in log_files
            ]
        }
        
        return summary


# Global session logger instance
_session_logger: Optional[SessionLogger] = None


def setup_session_logging(config) -> SessionLogger:
    """Setup session-based logging system."""
    global _session_logger
    
    _session_logger = SessionLogger(config)
    
    # Enable LiteLLM verbose logging if verbose mode is enabled
    if config.verbose:
        try:
            import litellm
            litellm.set_verbose = True
            logger.info("LiteLLM verbose logging enabled")
        except ImportError:
            logger.warning("LiteLLM not available for verbose logging")
    
    return _session_logger


def get_session_logger() -> Optional[SessionLogger]:
    """Get the current session logger."""
    return _session_logger


def create_agent_logger(context_id: str):
    """Create a specialized logger for agent execution traces."""
    if _session_logger:
        return _session_logger.create_agent_logger(context_id)
    else:
        return logger.bind(AGENT_TRACE=True, context_id=context_id)


def create_verbose_logger(name: str):
    """Create a logger that outputs to verbose debug log."""
    if _session_logger:
        return _session_logger.create_verbose_logger(name)
    else:
        return logger.bind(VERBOSE=True, logger_name=name)


def create_command_logger(command: str, project_name: str):
    """Create a logger for a specific command execution."""
    if _session_logger:
        return _session_logger.create_command_logger(command, project_name)
    else:
        return logger.bind(command=command, project_name=project_name), None 
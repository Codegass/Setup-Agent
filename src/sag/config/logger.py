"""Advanced logging system for SAG with session-based and verbose controls."""

import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Callable, Optional

from loguru import logger


def generate_session_id() -> str:
    """Generate a unique session ID.

    A random nonce separates consecutive sessions in one long-lived process;
    the pid suffix separates concurrent processes and remains useful in logs.
    Both are needed because the Workbench can force a new session logger
    without restarting Python.
    """

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"{timestamp}_{uuid.uuid4().hex[:12]}_{os.getpid()}"


class SessionLogger:
    """Manages session-specific logging with timestamp-based separation."""

    def __init__(self, config):
        self.config = config
        self.session_id = self._generate_session_id()
        self.base_log_dir = Path("logs")
        self.session_log_dir = self.base_log_dir / f"session_{self.session_id}"
        self._control_event_sink = None

        # Create log directories
        self.base_log_dir.mkdir(exist_ok=True)
        self.session_log_dir.mkdir(exist_ok=True)

        # Initialize loggers
        self._setup_loggers()

        logger.info(f"Session logging initialized. Session ID: {self.session_id}")
        logger.info(f"Session logs directory: {self.session_log_dir}")

    def _generate_session_id(self) -> str:
        """Generate a unique session ID based on timestamp and pid."""
        return generate_session_id()

    def _setup_loggers(self):
        """Setup all loggers with session-specific configuration."""

        # One console implementation, shared with the plain-CLI path. It clears
        # every existing handler first, so it has to run before the file sinks
        # below or it would wipe them.
        setup_console_logging(self.config)

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
                filter=lambda record: "VERBOSE" in record.get("extra", {})
                or record["level"].name in ["TRACE", "DEBUG"],
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

    def _get_file_format(self) -> str:
        """Get file log format."""
        return "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}"

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

    def get_control_event_sink(
        self,
        *,
        mirror: Optional[Callable[[str], None]] = None,
        clock: Optional[Callable[[], str]] = None,
        id_factory: Optional[Callable[[int], str]] = None,
        observers: tuple[Callable[[str], None], ...] = (),
    ):
        """Return the one append-only control stream owned by this session.

        The sink is built once and cached, so a later caller gets the stream
        that is already writing rather than a second one. What a later caller
        BRINGS still has to arrive: the CLI asks for this sink to hang a
        renderer on it before the agent is constructed, and the agent's
        container mirror therefore arrives on the second call. Dropping it
        there left `/workspace/.setup_agent/control_events.jsonl` — the copy
        `--record` archives — silently unwritten for the whole run.

        `clock` and `id_factory` are construction-only: changing either
        mid-stream would renumber or re-date a ledger that is already append-
        only, so a later call's copies are ignored.
        """
        from sag.agent.control_events import ControlEventSink

        if self._control_event_sink is None:
            self._control_event_sink = ControlEventSink(
                self.session_log_dir / "control_events.jsonl",
                mirror=mirror,
                clock=clock,
                id_factory=id_factory,
                observers=observers,
            )
            return self._control_event_sink

        if mirror is not None and not self._control_event_sink.attach_mirror(mirror):
            # Declining in silence is the exact shape of the defect this
            # method exists to end: a container ledger that is simply never
            # written, discoverable only once the container is gone.
            logger.warning(
                "This session's control stream already has a container mirror; "
                "the second one was declined and will record nothing."
            )
        for observer in observers:
            self._control_event_sink.add_observer(observer)
        return self._control_event_sink

    @property
    def run_pin_path(self) -> Path:
        return Path(self.session_log_dir) / "run-pin.json"

    def get_session_summary(self) -> dict:
        """Get a summary of the current logging session."""
        log_files = list(self.session_log_dir.glob("*.log"))

        summary = {
            "session_id": self.session_id,
            "session_dir": str(self.session_log_dir),
            "verbose_enabled": self.config.verbose,
            "log_level": self.config.log_level.value,
            "log_files": [
                {"name": f.name, "size": f.stat().st_size if f.exists() else 0, "path": str(f)}
                for f in log_files
            ],
        }

        return summary


# Global session logger instance
_session_logger: Optional[SessionLogger] = None
_session_logger_lock = RLock()


def setup_session_logging(config) -> SessionLogger:
    """Setup session-based logging system."""
    global _session_logger

    with _session_logger_lock:
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


# The console belongs to the turn stream now. Loguru keeps the files; on screen
# it speaks only when something went wrong, unless the user asked for more.
DEFAULT_CONSOLE_LEVEL = "WARNING"


def console_level(config) -> str:
    """The one rule for how loud the console is.

    `--verbose` is the way to ask the console for detail. `--log-level DEBUG`
    and `--log-level INFO` no longer open it; they still set the level the log
    files are written at.
    """

    if config.verbose:
        return "DEBUG"
    level = str(getattr(config.log_level, "value", config.log_level) or "").upper()
    if level in {"", "DEBUG", "INFO"}:
        return DEFAULT_CONSOLE_LEVEL
    return level


def setup_console_logging(config) -> None:
    """Configure the console sink. One implementation, used by both callers.

    The `logger.remove()` has to stay first: without it loguru's own DEBUG
    stderr handler survives, every warning prints twice, and the console is
    never quieted at all.
    """

    logger.remove()

    # No `filter=`: `level` is the whole rule. `console_level` can only yield
    # WARNING, ERROR, or DEBUG-when-verbose, so a record that clears the sink's
    # level is a record the console wants, and a second gate saying the same
    # thing only reads as though some case still needed it.
    logger.add(
        sys.stderr,
        level=console_level(config),
        format=_get_console_format(config),
        colorize=True,
    )


def _get_console_format(config) -> str:
    """Get console log format based on verbose setting."""
    if config.verbose:
        return (
            "<green>{time:HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        )
    return (
        "<green>{time:HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<level>{message}</level>"
    )


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

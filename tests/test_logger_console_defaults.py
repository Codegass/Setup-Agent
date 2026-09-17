"""The console shows turns; the files keep everything."""

import inspect
import subprocess
import sys
import textwrap
from pathlib import Path

from sag.config import logger as logger_module

REPO_ROOT = Path(__file__).resolve().parents[1]


def _config(*, verbose=False, log_level="INFO"):
    class _Config:
        pass

    cfg = _Config()
    cfg.verbose = verbose
    cfg.log_level = type("L", (), {"value": log_level})()
    return cfg


def test_there_is_one_console_implementation():
    source = inspect.getsource(logger_module.SessionLogger._setup_loggers)
    assert (
        "sys.stderr" not in source
    ), "the session logger must delegate its console sink, not add a second one"
    assert "setup_console_logging" in source


def test_the_console_default_is_warning():
    assert logger_module.console_level(_config()) == "WARNING"


def test_verbose_restores_debug():
    assert logger_module.console_level(_config(verbose=True)) == "DEBUG"


def test_asking_for_debug_or_info_leaves_the_console_quiet():
    """`--log-level DEBUG|INFO` no longer opens the console; `--verbose` does."""

    assert logger_module.console_level(_config(log_level="DEBUG")) == "WARNING"
    assert logger_module.console_level(_config(log_level="INFO")) == "WARNING"


def test_an_explicit_log_level_still_wins():
    assert logger_module.console_level(_config(log_level="ERROR")) == "ERROR"


def test_suppress_console_logging_is_gone():
    assert not hasattr(logger_module, "suppress_console_logging")


def test_the_action_line_is_logged_once():
    from sag.agent import react_engine

    source = inspect.getsource(react_engine)
    assert source.count('"🔧 ACTION: ') == 1, "the action line was reaching the console twice"


_PROBE = """
from loguru import logger

from sag.config.logger import setup_console_logging
from sag.config.settings import Config

setup_console_logging(Config())
logger.debug("PROBE-DEBUG")
logger.info("PROBE-INFO")
logger.warning("PROBE-WARNING")
"""


def test_the_console_sink_replaces_the_default_instead_of_stacking_on_it():
    """Drive the real process: loguru's own stderr handler must be gone."""

    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_PROBE)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert "PROBE-DEBUG" not in result.stderr
    assert "PROBE-INFO" not in result.stderr
    assert (
        result.stderr.count("PROBE-WARNING") == 1
    ), "a warning printed twice means two stderr sinks are open"

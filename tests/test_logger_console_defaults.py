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


_GIVE_WAY_PROBE = """
import sys

from loguru import logger

from sag.config.logger import before_each_console_line, setup_console_logging
from sag.config.settings import Config

setup_console_logging(Config())
before_each_console_line(lambda: sys.stderr.write("GAVE-WAY\\n"))
logger.warning("PROBE-WARNING")
before_each_console_line(None)
logger.warning("PROBE-AFTER")
"""


def test_the_console_sink_lets_the_turn_stream_finish_its_line_before_it_writes():
    """One screen, two writers: the log yields to the stream before every line.

    The turn stream holds a dispatch line open on stdout until the answer comes;
    a warning written to stderr in that window lands on the same screen line.
    The renderer cannot see another writer, so the console sink tells it first.
    Driven in a real process so the order on stderr is the order a terminal
    sees: the hook runs once per line, before the line, and not once cleared.
    """

    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_GIVE_WAY_PROBE)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr.count("GAVE-WAY") == 1, result.stderr
    assert result.stderr.index("GAVE-WAY") < result.stderr.index("PROBE-WARNING")
    assert result.stderr.index("PROBE-WARNING") < result.stderr.index("PROBE-AFTER")

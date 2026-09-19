"""The reader's setup report, rendered on the host from a finished run's record.

Every number asserted here was measured against the archived commons-cli run
that the fixture beside this file is a copy of: 19 turns, 19 tool calls,
368.4298 seconds, five phases, 129,567 model prompt tokens and 81,516 advisor
prompt tokens. The in-loop report tool, which reads the record twelve events
before the run ends, states 18 turns and 5m 59s for the same run — so a test
that passes here cannot pass against the document that tool wrote.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import time
from pathlib import Path

from sag.report_document import render_setup_report

FIXTURE = Path(__file__).parent / "fixtures" / "report_document" / "commons-cli"


def _session(tmp_path: Path) -> Path:
    """A writable copy of the archived run, for a test that renders into it."""

    session = tmp_path / "session_20260917_182955_763421_56a9bbfe481c_71905"
    shutil.copytree(FIXTURE, session)
    return session


@contextlib.contextmanager
def _clock_of(zone: str):
    """Read the document on the clock the run was recorded against.

    The run's timestamps are UTC and the document states them on the host's
    own clock, so an assertion about the hour is an assertion about a zone.
    Pinning it here is what keeps this test saying the same thing on a laptop
    in New York and a build machine in UTC.
    """

    before = os.environ.get("TZ")
    os.environ["TZ"] = zone
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


def test_the_document_states_the_run_that_finished():
    """The counts are the run's own, folded to its last event, not to turn 18."""

    document = render_setup_report(FIXTURE)

    assert "· 19 turns · 19 tool calls · 6m 08s |" in document
    # What the in-loop tool said about the same run, from twelve events short
    # of its end. Naming it here is what keeps the fix from silently reverting.
    assert "18 turns · 18 tool calls · 5m 59s" not in document


def test_the_document_names_the_run_and_the_commit_it_was_run_against():
    """One line of identity, one of provenance, both from the record."""

    document = render_setup_report(FIXTURE)
    lines = document.splitlines()

    assert lines[0] == "# commons-cli — setup report"
    assert "**Repository** https://github.com/apache/commons-cli.git at `e171117`" in document
    assert "**Run** `20260917_182955_763421_56a9bbfe481c_71905-7-0f1681c52e35`" in document
    # The project is named as the record names it: `.title()` made it Commons-Cli.
    assert "Commons-Cli" not in document


def test_the_document_is_dated_by_the_runs_last_event():
    """The run ended at 22:38:14Z; the report tool stamped its own turn, 18:38:04."""

    with _clock_of("America/New_York"):
        document = render_setup_report(FIXTURE)

    assert "written 2026-09-17 18:38:14" in document
    assert "18:38:04" not in document


def _session_log_dir(tmp_path: Path) -> Path:
    """The one session directory a CLI run under `tmp_path` wrote."""

    sessions = sorted((tmp_path / "logs").glob("session_*"))
    assert len(sessions) == 1, f"expected one session directory, found {sessions}"
    return sessions[0]


def test_the_run_end_writes_the_document_beside_the_record(monkeypatch, tmp_path):
    """Written whether or not the run was asked to keep artifacts.

    Without `--record` nothing is copied out of the container, so a document
    that waited for the copy would never be written for the majority of runs.
    """

    from test_cli_project_exit_codes import RecordingSetupAgent, invoke_project

    result = invoke_project(monkeypatch, tmp_path, RecordingSetupAgent)
    assert result.exit_code == 0, result.output

    written = sorted(_session_log_dir(tmp_path).glob("setup-report-*.md"))
    assert len(written) == 1, written
    document = written[0].read_text(encoding="utf-8")
    assert "## Result" in document
    assert "**Run** `cli-success`" in document


def test_the_archived_copy_does_not_overwrite_the_document(monkeypatch, tmp_path):
    """`--record` copies the container's stub out; the reader's report is last.

    The two write the same filename into the same directory, so whichever runs
    second is the document a reader opens. Ordering measured at the call site:
    the copy runs first, and this is what keeps it there.
    """

    from test_cli_project_exit_codes import RecordingSetupAgent, invoke_project

    import sag.main as main_module
    from sag.config import get_session_logger

    stub = "# 🎯 Project Setup Report v0.3.0\n\n**Generated:** 2026-09-17 18:38:04\n"

    def _copy_the_stub_out(orchestrator, project_name):
        del orchestrator, project_name
        target = get_session_logger().session_log_dir / "setup-report-20260917-183804.md"
        target.write_text(stub, encoding="utf-8")
        return str(target)

    monkeypatch.setattr(main_module, "_save_setup_artifacts", _copy_the_stub_out)

    result = invoke_project(monkeypatch, tmp_path, RecordingSetupAgent, "--record")
    assert result.exit_code == 0, result.output

    written = sorted(_session_log_dir(tmp_path).glob("setup-report-*.md"))
    assert [path.name for path in written] == ["setup-report-20260917-183804.md"]
    document = written[0].read_text(encoding="utf-8")
    assert "## Result" in document
    assert "Project Setup Report" not in document
    # The row names the file the reader is holding, not the container's copy.
    assert "| **Report** | delivered | setup-report-20260917-183804.md |" in document
    assert "/workspace/setup-report" not in document

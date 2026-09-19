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


def test_the_document_says_what_was_provisioned():
    """Turns 3 and 4 installed a JDK and a Maven; the old report named neither."""

    document = render_setup_report(FIXTURE)

    assert "## What was set up" in document
    assert "| java | 1.8.0_502 | `/usr/lib/jvm/java-8-openjdk-amd64/jre/bin/java` |" in document
    assert "| maven | 3.9.9 | `/opt/apache-maven-3.9.9/bin/mvn` |" in document
    assert "installed by the run" in document


def test_a_run_that_provisioned_nothing_says_nothing(tmp_path):
    """A heading with nothing under it is a question a reader cannot answer."""

    session = _session(tmp_path)
    (session / ".setup_agent" / "env_overlay.json").write_text(
        '{"tools": {}, "version": 1}', encoding="utf-8"
    )

    assert "## What was set up" not in render_setup_report(session)


def test_the_document_carries_one_row_per_turn():
    """The 19-line trajectory is the best summary of the run that exists."""

    document = render_setup_report(FIXTURE)
    lines = document.splitlines()
    start = lines.index("## The run")
    section = lines[start:]

    assert "19 turns across 5 phases, 6m 08s." in section
    for band in ("**provision**", "**analyze**", "**build**", "**test**", "**report**"):
        assert band in section, band
    numbered = [
        line for line in section if line.startswith("| ") and line[2:].split(" ")[0].isdigit()
    ]
    assert len(numbered) == 19

    # Each turn's own span, opened by its call and closed by its turn record —
    # `sag trajectory`'s replay prints a shorter one because it renders a turn
    # at its tool result, before the record that closes the turn has landed.
    assert (
        "| 1 | project | clone apache/commons-cli | e171117 → /workspace/commons-cli | 4.8s |"
        in section
    )
    assert (
        "| 12 | build | verify mvn -B clean verify "
        "| exit 0 · 994 tests · 0 F · 0 E · 61 S · 6 artifacts | 2m06s |"
    ) in section
    # A turn with nothing to say about what came back still came back.
    assert "| 4 | project | provision maven 3.9.9 | ok | 6.9s |" in section


def test_the_turns_the_harness_took_are_marked_as_its_own():
    """Turns 11 and 14 consulted the advisor because the harness made them."""

    document = render_setup_report(FIXTURE)

    assert "| 11 | advisor | consult (the harness asked) |" in document
    assert "| 14 | advisor | consult (the harness asked) |" in document
    # Turn 2 was the same call, asked for by the model.
    assert "| 2 | advisor | consult |" in document


def test_the_document_bills_the_model_and_the_advisor_apart():
    """Two models answered; one added total would say the first spent it all."""

    document = render_setup_report(FIXTURE)

    assert "## Tokens" in document
    assert "| **Model** | gpt-5.4-mini | 14 | 129,567 | 1,932 |" in document
    # This run's pin records the advisor's mode (`same-model`) and not its
    # model, so the cell states the absence rather than guessing the name.
    assert "| **Advisor** | — | 3 | 81,516 | 477 |" in document
    assert "211,083" not in document


def test_a_run_that_consulted_nobody_bills_no_advisor(tmp_path):
    """The advisor row is a bill, and an unsent bill is not printed as zero."""

    session = _session(tmp_path)
    ledger = session / "control_events.jsonl"
    kept = [
        line for line in ledger.read_text(encoding="utf-8").splitlines() if '"advisor"' not in line
    ]
    ledger.write_text("\n".join(kept) + "\n", encoding="utf-8")

    document = render_setup_report(session)

    assert "| **Model** |" in document
    assert "| **Advisor** |" not in document


def test_the_document_counts_evidence_instead_of_listing_it():
    """The old report joined 81 paths into one 6,215-character line."""

    document = render_setup_report(FIXTURE)
    over_long = [line for line in document.splitlines() if len(line) > 200]

    assert not over_long, over_long
    assert (
        "The run cited 72 distinct artifacts: 47 surefire report files, "
        "13 stored tool outputs, 6 compiled classes, 4 jars, "
        "1 validator observation, 1 workspace directory."
    ) in document
    assert "AlreadySelectedExceptionTest.xml" not in document


def test_the_document_says_how_to_open_the_evidence():
    """A reader on the host reaches the record with commands, not with paths."""

    document = render_setup_report(FIXTURE)

    assert f"uv run sag trajectory {FIXTURE}" in document
    # The turn named is the one that produced the build receipt, read from the
    # trajectory rather than written down here.
    assert f"uv run sag inspect commons-cli --session {FIXTURE} --turn 12" in document
    assert f"uv run sag result {FIXTURE}" in document
    assert "uv run sag ui" in document


def test_the_document_keeps_the_accounting_and_the_static_count():
    """The audit trail behind the Tests row, plus the one number it was missing."""

    document = render_setup_report(FIXTURE)
    lines = document.splitlines()
    section = lines[lines.index("## Evidence accounting") :]

    assert (
        "- Test classes identified by module and name: 534/595 passed, 0 failed, "
        "0 errors, 61 skipped" in section
    )
    assert (
        "- Tests identified by module and name: 933/994 passed, 0 failed, "
        "0 errors, 61 skipped" in section
    )
    assert (
        "- Results bound to this run's receipts: 933/994 passed, 0 failed, "
        "0 errors, 61 skipped" in section
    )
    assert (
        "- Set aside: not from this run's receipts: 0/0 passed, 0 failed, 0 errors, 0 skipped"
        in section
    )
    assert (
        "- Set aside: no module or test name recorded: 0/0 passed, 0 failed, 0 errors, 0 skipped"
        in section
    )
    assert "- Set aside: from an earlier run: 0/0 passed, 0 failed, 0 errors, 0 skipped" in section
    assert "- Evidence records: complete" in section
    # Moved here from the diagnostics table the document drops: it is the one
    # fact that table carried which the Tests row does not.
    assert (
        "- Static test declarations found by analysis: 472 "
        "(diagnostic; not the denominator above)" in section
    )

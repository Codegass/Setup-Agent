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


def _recording_agent():
    """A run that leaves a ledger behind, the way a real one does.

    The CLI fake seals a verdict but writes no control events, and the
    document's account of the run is read from those. This subclass drops the
    archived run's ledger into the host session directory as the session
    logger would have, so the fence below reads a document with a run in it.
    """

    from test_cli_project_exit_codes import RecordingSetupAgent

    from sag.config import get_session_logger

    class _LedgerWritingAgent(RecordingSetupAgent):
        def setup_project(self, **kwargs):
            termination = super().setup_project(**kwargs)
            session = get_session_logger().session_log_dir
            session.mkdir(parents=True, exist_ok=True)
            shutil.copy(FIXTURE / "control_events.jsonl", session / "control_events.jsonl")
            return termination

    return _LedgerWritingAgent


def test_the_run_end_writes_the_document_beside_the_record(monkeypatch, tmp_path):
    """Written whether or not the run was asked to keep artifacts.

    Without `--record` nothing is copied out of the container, so a document
    that waited for the copy would never be written for the majority of runs.
    """

    from test_cli_project_exit_codes import invoke_project

    result = invoke_project(monkeypatch, tmp_path, _recording_agent())
    assert result.exit_code == 0, result.output

    written = sorted(_session_log_dir(tmp_path).glob("setup-report-*.md"))
    assert len(written) == 1, written
    document = written[0].read_text(encoding="utf-8")
    assert "## Result" in document
    assert "**Run** `cli-success`" in document
    # The account of the run is in the file the run left behind, which is what
    # a document written before the run ended could never carry.
    assert "## The run" in document
    assert "19 turns across 5 phases" in document


def test_the_archived_copy_does_not_overwrite_the_document(monkeypatch, tmp_path):
    """`--record` copies the container's stub out; the reader's report is last.

    The two write the same filename into the same directory, so whichever runs
    second is the document a reader opens. Ordering measured at the call site:
    the copy runs first, and this is what keeps it there.
    """

    from test_cli_project_exit_codes import invoke_project

    import sag.main as main_module
    from sag.config import get_session_logger

    stub = "# 🎯 Project Setup Report v0.3.0\n\n**Generated:** 2026-09-17 18:38:04\n"

    def _copy_the_stub_out(orchestrator, project_name):
        del orchestrator, project_name
        target = get_session_logger().session_log_dir / "setup-report-20260917-183804.md"
        target.write_text(stub, encoding="utf-8")
        return str(target)

    monkeypatch.setattr(main_module, "_save_setup_artifacts", _copy_the_stub_out)

    result = invoke_project(monkeypatch, tmp_path, _recording_agent(), "--record")
    assert result.exit_code == 0, result.output

    written = sorted(_session_log_dir(tmp_path).glob("setup-report-*.md"))
    assert [path.name for path in written] == ["setup-report-20260917-183804.md"]
    document = written[0].read_text(encoding="utf-8")
    assert "## Result" in document
    assert "## The run" in document
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
    # The pin records the advisor's mode rather than its model, and
    # `same-model` names the model as plainly as a second copy of the name
    # would: this run consulted the model it was already running.
    assert "| **Advisor** | gpt-5.4-mini | 3 | 81,516 | 477 |" in document
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


def test_the_document_is_a_reading_of_whatever_record_it_is_handed(tmp_path):
    """The staleness was the reading instant, never the renderer.

    Hand this renderer the ledger as it stood when the report tool ran — 110
    of the run's 122 events — and it states exactly what that tool stated. The
    fix is that it is handed the finished record instead, which is why it
    takes a session directory and can never be handed a running engine.
    """

    import inspect

    session = _session(tmp_path)
    ledger = session / "control_events.jsonl"
    at_the_report_turn = ledger.read_text(encoding="utf-8").splitlines()[:110]
    ledger.write_text("\n".join(at_the_report_turn) + "\n", encoding="utf-8")

    assert "· 18 turns · 18 tool calls · 5m 59s |" in render_setup_report(session)

    parameters = inspect.signature(render_setup_report).parameters
    assert next(iter(parameters)) == "session_dir"
    # Every other input names an artifact the run wrote. Nothing here can be
    # handed a live engine, a report tool, or a container.
    assert set(parameters) == {
        "session_dir",
        "card",
        "verdict",
        "report_metrics",
        "project_meta",
        "run_pin",
        "env_overlay",
    }


def test_the_document_is_written_in_plain_english():
    """Nine words name house concepts a reader of a report does not share."""

    import re

    forbidden = (
        "sealed",
        "canonical",
        "claimed",
        "quarantined",
        "subject",
        "snapshot",
        "metrics-v2",
        "verdict-bearing",
        "promoting",
    )
    document = render_setup_report(FIXTURE)
    # A raw reason code quoted in brackets is the handle a reader searches
    # for, and is exempt; the prose around it is not.
    prose = re.sub(r"\([A-Za-z0-9_]+\)", "", document)

    found = [word for word in forbidden if word in prose.lower()]
    assert not found, f"house vocabulary in the reader's report: {found}"


class _RecordingContainer:
    """A container that answers the report lookup and keeps what it is sent."""

    def __init__(self, stub: str | None = None):
        self.stub = stub
        self.commands: list[str] = []

    def execute_command(self, command, **kwargs):
        del kwargs
        self.commands.append(command)
        if command.startswith("find /workspace"):
            return {"exit_code": 0, "output": self.stub or ""}
        return {"exit_code": 0, "output": ""}


def test_the_document_replaces_the_stub_inside_the_container():
    """One run, one report: the container's copy is the host's copy."""

    import sag.main as main_module

    container = _RecordingContainer(stub="/workspace/setup-report-20260917-183804.md")
    main_module._write_report_into_container(
        container, "# commons-cli — setup report\n", name="setup-report-20260917-183900.md"
    )

    written = [line for line in container.commands if line.startswith("cat > ")]
    assert len(written) == 1, container.commands
    assert written[0].startswith("cat > /workspace/setup-report-20260917-183804.md << ")
    assert "# commons-cli — setup report" in written[0]


def test_a_container_that_kept_no_report_is_given_this_runs_name():
    """A run whose report phase wrote nothing still ends up holding the report."""

    import sag.main as main_module

    container = _RecordingContainer()
    main_module._write_report_into_container(
        container, "# doc\n", name="setup-report-20260917-183900.md"
    )

    written = [line for line in container.commands if line.startswith("cat > ")]
    assert written[0].startswith("cat > /workspace/setup-report-20260917-183900.md << ")


def test_the_run_end_hands_the_container_the_document_it_wrote(monkeypatch, tmp_path):
    """The bytes the host wrote are the bytes the container is sent."""

    from test_cli_project_exit_codes import RecordingSetupAgent, invoke_project

    import sag.main as main_module

    sent: list[tuple[str, str]] = []
    real = main_module._write_report_into_container

    def _spy(orchestrator, document, *, name, **rest):
        sent.append((document, name))
        return real(orchestrator, document, name=name, **rest)

    monkeypatch.setattr(main_module, "_write_report_into_container", _spy)
    result = invoke_project(monkeypatch, tmp_path, RecordingSetupAgent)
    assert result.exit_code == 0, result.output

    written = sorted(_session_log_dir(tmp_path).glob("setup-report-*.md"))
    assert len(sent) == 1, sent
    assert sent[0][0] == written[0].read_text(encoding="utf-8")
    assert sent[0][1] == written[0].name


def test_the_write_back_never_fails_the_run(monkeypatch, tmp_path):
    """A container that has gone away costs the run nothing; the host has it."""

    from test_cli_project_exit_codes import RecordingSetupAgent, invoke_project

    import sag.main as main_module

    def _refuse(orchestrator, document, *, name, **rest):
        raise RuntimeError("the container is gone")

    monkeypatch.setattr(main_module, "_write_report_into_container", _refuse)

    result = invoke_project(monkeypatch, tmp_path, RecordingSetupAgent)

    assert result.exit_code == 0, result.output
    written = sorted(_session_log_dir(tmp_path).glob("setup-report-*.md"))
    assert len(written) == 1, written
    assert "## Result" in written[0].read_text(encoding="utf-8")


def test_the_web_tab_reads_the_documents_own_timestamp():
    """The report tab dates the document, and the session list times the run.

    Both read the time out of the document's own text. The reader's report
    carries it on the `**Run**` line rather than in the `**Generated:**` line
    the in-loop tool wrote, and both spellings are read so an archived run
    still dates correctly.
    """

    from sag.web.session_registry import _report_document, _report_generated_at

    document = render_setup_report(FIXTURE)

    assert _report_generated_at(document) == "2026-09-17 18:38:14"
    assert _report_generated_at("**Generated:** 2026-09-17 18:38:04\n") == "2026-09-17 18:38:04"

    rendered = _report_document(
        {"report_path": "/workspace/setup-report-20260917-183804.md", "report_raw": document}
    )
    assert rendered is not None
    assert rendered.generated == "2026-09-17 18:38:14"
    # The Result table still reaches the tab as a table, not as prose.
    tables = [block for block in rendered.blocks if block.get("type") == "table"]
    assert any(any(row and row[0] == "Setup" for row in table["rows"]) for table in tables), tables


def test_the_document_counts_the_phases_the_run_ran():
    """The run ran five phases; its verdict was finalized during the fourth.

    The verdict is written when the evidence closes, before the report phase
    exists, so its phase records hold four. The trajectory bands five, and
    five is what the run did — on every surface at once, because all three
    read the same group of counts.
    """

    document = render_setup_report(FIXTURE)

    assert "| **Setup** | success | 5/5 phases · 19 turns · 19 tool calls · 6m 08s |" in document
    assert "4/4 phases" not in document


def test_a_session_directory_that_is_gone_costs_the_run_nothing(monkeypatch, tmp_path):
    """Naming a document is not worth a run's exit code.

    Everything about the report at run end is best-effort, and the step that
    decides what to call the file reads the ledger to date it. A directory
    that is not there raised out of that read, past the guard around the
    write, into the command's own handler: a successful run printed
    `❌ Setup failed` and exited 1 because a document could not be named.
    """

    import shutil

    from test_cli_project_exit_codes import invoke_project

    import sag.main as main_module
    from sag.config import get_session_logger

    def _take_the_directory_away(orchestrator, project_name):
        del orchestrator, project_name
        shutil.rmtree(get_session_logger().session_log_dir)
        return None

    monkeypatch.setattr(main_module, "_save_setup_artifacts", _take_the_directory_away)

    result = invoke_project(monkeypatch, tmp_path, _recording_agent(), "--record")

    assert result.exit_code == 0, result.output
    assert "Setup failed" not in result.output
    assert " Setup         success" in result.output


def test_the_block_names_the_document_only_when_it_was_written(monkeypatch, tmp_path):
    """A row that names a file the reader cannot open is worse than no name.

    The card used to be built from the path the run *intended* to write, and
    the write's answer was thrown away — so a failed write still printed
    `Report | delivered | setup-report-….md` for a file that is not there.
    """

    from test_cli_project_exit_codes import invoke_project

    import sag.main as main_module

    def _refuse(*args, **kwargs):
        raise RuntimeError("nothing could be rendered")

    monkeypatch.setattr(main_module, "render_setup_report", _refuse)

    result = invoke_project(monkeypatch, tmp_path, _recording_agent())

    assert result.exit_code == 0, result.output
    assert not sorted(_session_log_dir(tmp_path).glob("setup-report-*.md"))
    assert "setup-report-" not in result.output
    # What the run itself recorded: the report tool wrote one in the container.
    assert "written inside the container" in result.output


def test_both_surfaces_say_delivered_and_name_the_same_file(monkeypatch, tmp_path):
    """The block and the document state one file, found the one way."""

    from test_cli_project_exit_codes import invoke_project

    result = invoke_project(monkeypatch, tmp_path, _recording_agent())
    assert result.exit_code == 0, result.output

    written = sorted(_session_log_dir(tmp_path).glob("setup-report-*.md"))
    assert len(written) == 1, written
    document = written[0].read_text(encoding="utf-8")

    assert f"| **Report** | delivered | {written[0].name} |" in document
    assert f"Report        delivered      {written[0].name}" in result.output


def test_the_writer_and_the_web_mean_the_same_container_report():
    """A run whose model called the report tool twice leaves two of them.

    The report tool never removes an older `setup-report-<ts>.md`. The web
    resolved the newest and the write-back the first line `find` happened to
    print, so the host document could land on the older file while the Report
    tab went on showing the stub.
    """

    import sag.main as main_module
    from sag.web.session_registry import _latest_setup_report_path

    two = "/workspace/setup-report-20260917-183804.md\n/workspace/setup-report-20260917-184500.md"

    container = _RecordingContainer(stub=two)
    main_module._write_report_into_container(container, "# doc\n", name="setup-report-x.md")
    written = [line for line in container.commands if line.startswith("cat > ")]

    assert written[0].startswith("cat > /workspace/setup-report-20260917-184500.md << ")
    assert _latest_setup_report_path(_RecordingContainer(stub=two)) == (
        "/workspace/setup-report-20260917-184500.md"
    )
    # Both ask the container to choose, in the same words, so the container's
    # answer cannot depend on which surface asked.
    asked = [line for line in container.commands if line.startswith("find /workspace")]
    assert asked and "sort | tail -1" in asked[0]


def test_the_host_and_the_container_call_the_report_one_name(monkeypatch, tmp_path):
    """Without `--record` nothing is copied out, and the two names diverged.

    The host file was dated from the run's last event and the container's stub
    from the clock at the report turn, so one run left `setup-report-A.md`
    beside the ledger and `setup-report-B.md` in the container.
    """

    from test_cli_project_exit_codes import invoke_project

    import sag.main as main_module

    sent: list[str] = []
    real = main_module._write_report_into_container

    def _spy(orchestrator, document, *, name, **rest):
        sent.append(name)
        return real(orchestrator, document, name=name, **rest)

    monkeypatch.setattr(main_module, "_write_report_into_container", _spy)
    monkeypatch.setattr(
        main_module,
        "container_report_path",
        lambda orchestrator: "/workspace/setup-report-20260917-183804.md",
    )

    result = invoke_project(monkeypatch, tmp_path, _recording_agent())
    assert result.exit_code == 0, result.output

    written = sorted(_session_log_dir(tmp_path).glob("setup-report-*.md"))
    assert [path.name for path in written] == ["setup-report-20260917-183804.md"]
    assert sent == ["setup-report-20260917-183804.md"]


def test_the_document_names_the_tools_the_run_would_not_use(tmp_path):
    """The run that could not provision a JDK is the run this section is for.

    The section used to print only tools with an active executable, so a run
    that found a JDK and refused it — the case where what was set up is the
    whole story — printed no `## What was set up` at all.
    """

    import json

    session = _session(tmp_path)
    (session / ".setup_agent" / "env_overlay.json").write_text(
        json.dumps(
            {
                "version": 1,
                "tools": {
                    "java": {
                        "active": None,
                        "blocked": [
                            {
                                "executable": "/usr/bin/java",
                                "version": "11.0.22",
                                "requirement": ">=17",
                                "reason": "the build asked for a newer Java than this one",
                                "source": "build_error",
                            }
                        ],
                        "candidates": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    document = render_setup_report(session)

    assert "## What was set up" in document
    assert "### Tools the run did not use" in document
    assert (
        "| java | 11.0.22 | `/usr/bin/java` | >=17 "
        "| the build asked for a newer Java than this one |"
    ) in document


def test_a_run_that_refused_nothing_prints_no_second_table():
    """Every table in the document is a table with rows in it."""

    document = render_setup_report(FIXTURE)

    assert "## What was set up" in document
    assert "did not use" not in document


def test_only_the_workspace_itself_is_counted_as_a_directory():
    """A path with no extension is not the same thing as a directory."""

    from sag.report_document.render import classify_evidence_ref

    roots = ("/workspace", "/workspace/commons-cli")

    assert classify_evidence_ref("/workspace/commons-cli", roots) == "directory"
    assert classify_evidence_ref("/workspace", roots) == "directory"
    # Files the build reads and writes that happen to carry no extension.
    assert classify_evidence_ref("/workspace/commons-cli/Makefile", roots) == "other"
    assert classify_evidence_ref("/workspace/commons-cli/mvnw", roots) == "other"
    assert classify_evidence_ref("/workspace/commons-cli/target/classes", roots) == "other"


def test_a_citation_that_names_two_files_is_counted_as_neither():
    """Folding a spelling into the wrong file would undercount the run's work."""

    from sag.report_document.render import artifacts_by_kind

    counted = artifacts_by_kind(
        [
            "/workspace/p/one/target/app.jar",
            "/workspace/p/two/target/app.jar",
            "target/app.jar",
        ],
        roots=("/workspace/p",),
    )

    # Two jars, and one citation that could be either of them.
    assert counted == {"jar": 2, "other": 1}


def test_the_evidence_sentence_still_adds_up_for_the_archived_run():
    """The counts are a census of the artifacts, so they sum to the total."""

    document = render_setup_report(FIXTURE)

    assert (
        "The run cited 72 distinct artifacts: 47 surefire report files, "
        "13 stored tool outputs, 6 compiled classes, 4 jars, "
        "1 validator observation, 1 workspace directory."
    ) in document


def test_a_half_recorded_token_bill_is_still_printed():
    """One side of a bill missing is not a reason to lose the whole document.

    `_totalled` answers with whichever side the rows supplied, so a run whose
    completion tokens were never recorded carries an input total and no output
    total. Formatting both together raised, the guard around the write caught
    it, and the run ended with no document at all.
    """

    from sag.report_document.render import render_setup_report as render
    from sag.result_card import build_result_card
    from sag.result_card.run_evidence import read_run_counts

    counts = read_run_counts(FIXTURE)
    counts["token_usage"] = [{"prompt_tokens": 1234, "output_tokens": None}]
    counts["advisor_token_usage"] = None
    card = build_result_card(
        __import__("json").loads(
            (FIXTURE / ".setup_agent" / "verdict.json").read_text(encoding="utf-8")
        ),
        **counts,
    )

    document = render(FIXTURE, card=card)

    assert "| **Model** | — | 14 | 1,234 | — |" in document
    assert "| **Advisor** |" not in document


def _repaired_run():
    """A run that was sent back to the phase it was already in.

    Two bands with one name and no other phase between them: the reducer
    opens a second band on the repair, and nothing in a turn's own fields
    says which of the two it belongs to. The gate each band was decided by
    does say, because the turn that carried that gate is in the ledger.
    """

    from sag.trajectory.schema import (
        CallInfo,
        GateInfo,
        PhaseInfo,
        SessionInfo,
        Trajectory,
        Turn,
    )

    def _turn(turn_id, summary, gate=None):
        return Turn(
            turn_id=turn_id,
            phase="build",
            actor="model",
            call=CallInfo(tool="build", summary=summary),
            gate=GateInfo(word="failed" if gate == "g1" else "success", decision_id=gate)
            if gate
            else None,
        )

    return Trajectory(
        session=SessionInfo(run_id="repaired", wall_clock_seconds=12.0),
        phases=[
            PhaseInfo(
                name="build",
                termination="repair",
                gates=[GateInfo(word="failed", decision_id="g1")],
            ),
            PhaseInfo(
                name="build",
                termination="advance",
                gates=[GateInfo(word="success", decision_id="g2")],
            ),
        ],
        turns=[
            _turn(1, "verify mvn -B verify", gate="g1"),
            _turn(2, "verify mvn -B -pl core verify"),
            _turn(3, "done success", gate="g2"),
        ],
    )


def test_a_phase_visited_twice_gets_a_table_each_time():
    """The account of the run is banded the way the run's own record bands it.

    Grouping by consecutive phase names merged the two visits into one table
    while the sentence above counted two phases — one more phase than there
    were tables to show for it.
    """

    from sag.report_document.render import _the_run

    lines = _the_run(_repaired_run())
    text = "\n".join(lines)

    assert "3 turns across 2 phases, 12.0s." in text
    assert text.count("**build**") == 2
    assert text.count("| # | Tool | Asked | Result | Took |") == 2
    first, second = text.split("**build**")[1:]
    assert "| 1 | build |" in first and "| 2 | build |" not in first
    assert "| 2 | build |" in second and "| 3 | build |" in second


def test_the_web_tab_shows_the_commands_as_commands():
    """Four commands to copy, not four paragraphs of prose.

    The tab's parser had no branch for an indented block, so each command in
    "To open it" arrived as its own paragraph, wrapped and proportional — a
    reader could not tell where one command ended.
    """

    from sag.web.session_registry import _report_blocks

    blocks = _report_blocks(render_setup_report(FIXTURE))
    code = [block for block in blocks if block.get("type") == "code"]

    assert len(code) == 1, [block.get("type") for block in blocks]
    commands = code[0]["text"].splitlines()
    assert commands[0] == f"uv run sag trajectory {FIXTURE}"
    assert commands[-1] == "uv run sag ui"
    # The band labels stay prose; only the block a reader would copy is code.
    assert any(block.get("text") == "provision" for block in blocks)
    assert not any(
        isinstance(block.get("text"), str) and block["text"].startswith("uv run")
        for block in blocks
        if block.get("type") == "p"
    )


def test_sag_result_answers_which_model_the_advisor_was():
    """The card carries it, so every surface that reads the card has it."""

    import json

    from click.testing import CliRunner

    import sag.main as main_module

    result = CliRunner().invoke(main_module.cli, ["result", str(FIXTURE), "--json"])

    assert result.exit_code == 0, result.output
    card = json.loads(result.output)
    assert card["stats"]["model"] == "gpt-5.4-mini"
    assert card["stats"]["advisor_model"] == "gpt-5.4-mini"

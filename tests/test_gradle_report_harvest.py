# tests/test_gradle_report_harvest.py
"""Plan — a Gradle run states what it left on disk.

Measured (`docs/superpowers/reports/gradle-evidence-20260830.md`): the kafka
d2r6 container held 1,176 JUnit XML files, 19 modules, 27,219 executed tests
and 8 failures. The receipt for that run carried `module_outcomes` and nothing
else — no counts, no identities, every sealed number `unavailable`. Its largest
single report is 137.8 MB against a 16 MB receipt canonical budget, so the
transport that reports it can never read a report whole.

The fixtures under `fixtures/gradle_receipts/` are that evidence, cut small:
two real kafka suites (one carrying a `<failure>`, one green), the first 4 KB
of the 137.8 MB streams report, and geode's real `test-results` directory
listing — which is what proves the discovery glob must find `distributedTest`
beside `test` and must never descend into `binary/`.
"""

import json
import shlex
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest
from test_container_io import FakeContainer as ContainerFilesystem
from test_invocation_receipts import (
    ReceiptOrchestrator,
    argv_contract_authority,
    ok,
    receipts_written,
)

from sag.agent.invocation_receipts import (
    DECLARED_OMISSION_REASONS,
    GRADLE_DISCOVERY_INCOMPLETE,
    GRADLE_NO_CLAIMED_TEST_REPORTS,
    GRADLE_NO_TEST_REPORTS,
    GRADLE_ROW_SAMPLE_UNREADABLE,
    GRADLE_SUITE_TOTALS_UNREADABLE,
    TESTCASE_FILE_CAP,
    build_receipt,
    parse_report_tag_rows,
    report_tag_command,
    validate_receipt_v2,
)
from sag.tools.internal.gradle_tool import (
    _GRADLE_SUITE_HEAD_READER,
    GRADLE_SUITE_HEAD_BYTES,
    GRADLE_XML_FILE_CAP,
    GradleTool,
    _gradle_discover_reports,
    _gradle_module_outcomes,
    _gradle_module_outcomes_with_counts,
    _gradle_red_first,
    _gradle_report_identity,
    gradle_test_action,
    gradle_test_harvest,
)

FIXTURES = Path(__file__).parent / "fixtures" / "gradle_receipts"
ROOT = "/workspace/kafka"
COUNT_MARKER = "###sag-gradle-xml-count###"
REPORT_MARKER = "###sag-report###"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _digest(name: str) -> str:
    return sha256(_fixture(name)).hexdigest()


def _geode_report_paths() -> list:
    """geode's real listing, rooted where a dispatch would have found it."""
    lines = (FIXTURES / "geode-test-results-listing.txt").read_text().splitlines()
    return [f"/workspace/{line}" for line in lines if line.endswith(".xml")]


class FakeContainer(ContainerFilesystem):
    """A real bounded-write filesystem that also answers the harvest's reads.

    The atomic path-list write is the shared one every evidence transport uses,
    so it is exercised, not stubbed: report paths must reach the reader through
    a file, never through argv.
    """

    def __init__(self, *, discovery=None, heads=None, tags=None):
        super().__init__()
        self.discovery = discovery
        self.heads = heads
        self.tags = tags
        self.head_input = None

    def execute_command(self, command, **kwargs):
        if COUNT_MARKER in command:
            self.commands.append(command)
            return self._reply(self.discovery)
        if "ATTRIBUTE = re.compile" in command:
            self.commands.append(command)
            self.head_input = json.loads(self.files[shlex.split(command)[-1]])
            return self._reply(self._head_reply())
        if REPORT_MARKER in command:
            self.commands.append(command)
            return self._reply(self.tags)
        return super().execute_command(command, **kwargs)

    def _head_reply(self):
        """A dict of heads answers only for the paths it was ASKED about.

        The reader opens the files the harvest handed it and no others, so a
        double that answered for a file nobody asked about would hide the whole
        question of which files the harvest chooses to read.
        """
        if not isinstance(self.heads, dict):
            return self.heads
        return _head_output(
            [{"path": path, **self.heads[path]} for path in self.head_input if path in self.heads]
        )

    @staticmethod
    def _reply(payload):
        if payload is None:
            return {"exit_code": 1, "output": "", "success": False}
        return {"exit_code": 0, "output": payload, "success": True}


def _discovery_output(paths, total=None, status="0"):
    """The pipeline's own output: the bounded list, the true total, find's status."""
    body = "\n".join(paths)
    return f"{body}\n{COUNT_MARKER}{total if total is not None else len(paths)} {status}\n"


def _head_output(entries):
    return json.dumps({"status": "complete", "suites": entries})


def _delta(*names_and_paths):
    return {
        "new": [{"path": path, "sha256": _digest(name)} for name, path in names_and_paths],
        "changed": [],
    }


# --- discovery -------------------------------------------------------------


def test_discovery_globs_every_task_dir_and_never_descends_into_binary():
    """geode writes `distributedTest` beside `test`, and `binary/` beside both.

    A glob hard-coded to `test-results/test` would have missed geode's
    distributed suites entirely, and one that did not exclude `binary/` would
    hand Gradle's internal result store to a report parser.
    """
    container = FakeContainer(discovery=_discovery_output(["/workspace/geode/x.xml"]))

    _gradle_discover_reports(container.execute_command, "/workspace")

    command = container.commands[0]
    assert "'*/build/test-results/*/*.xml'" in command
    assert "! -path '*/binary/*'" in command
    # The listing is real: both task dirs exist under one project's build dir.
    listing = (FIXTURES / "geode-test-results-listing.txt").read_text()
    assert "geode-core/build/test-results/distributedTest" in listing
    assert "geode-core/build/test-results/test/binary" in listing


def test_every_geode_report_resolves_to_its_own_project_and_task_dir():
    identities = {
        _gradle_report_identity(path, "/workspace/geode") for path in _geode_report_paths()
    }

    assert None not in identities
    assert all(task == "test" for _module, task in identities)
    assert (":geode-core", "test") in identities
    # A nested project keeps every path segment: `extensions/geode-modules` is
    # `:extensions:geode-modules`, never `:geode-modules`.
    assert (":extensions:geode-modules", "test") in identities
    assert _gradle_report_identity(
        "/workspace/geode/geode-core/build/test-results/distributedTest/TEST-a.xml",
        "/workspace/geode",
    ) == (":geode-core", "distributedTest")


def test_a_scan_that_never_reached_its_marker_knows_nothing():
    """An unfinished scan must not be readable as "no reports here"."""
    container = FakeContainer(discovery="/workspace/a.xml\n/workspace/b.xml\n")

    discovery = _gradle_discover_reports(container.execute_command, ROOT)

    assert discovery.complete is False
    assert discovery.paths == () and discovery.total == 0


def test_the_file_bound_hands_back_a_capped_list_and_the_true_total():
    paths = [f"{ROOT}/m/build/test-results/test/TEST-{index}.xml" for index in range(3)]
    container = FakeContainer(discovery=_discovery_output(paths, total=GRADLE_XML_FILE_CAP + 9))

    discovery = _gradle_discover_reports(container.execute_command, ROOT)

    assert discovery.complete is True
    assert len(discovery.paths) == 3
    assert discovery.total == GRADLE_XML_FILE_CAP + 9
    assert f"count < {GRADLE_XML_FILE_CAP}" in container.commands[0]


def test_a_total_smaller_than_the_listing_is_a_broken_scan_not_a_short_one():
    container = FakeContainer(discovery=_discovery_output(["/a.xml", "/b.xml"], total=1))

    assert _gradle_discover_reports(container.execute_command, ROOT).complete is False


def test_a_scan_whose_find_failed_is_unfinished_however_cleanly_awk_ended():
    """The marker is `awk`'s statement, and `awk` always gets to make it.

    `find`'s errors go to `/dev/null` and the pipeline's exit code belongs to
    the last stage, so an END block that ran is no evidence the tree was read.
    A status the scan itself did not state is the same non-answer.
    """
    failed = FakeContainer(discovery=_discovery_output([], total=0, status="1"))
    unstated = FakeContainer(discovery=f"{COUNT_MARKER}0\n")

    assert _gradle_discover_reports(failed.execute_command, ROOT).complete is False
    assert _gradle_discover_reports(unstated.execute_command, ROOT).complete is False


class ShellContainer:
    """Runs the harvest's own command through a REAL shell, on this machine.

    The discovery pass is a shell pipeline, and every claim it makes about
    completeness is a claim about how that pipeline composes `find`, `sort` and
    `awk`. A double can only ever confirm the parse; this confirms the command.
    """

    def __init__(self):
        self.commands = []

    def execute_command(self, command, **kwargs):
        del kwargs
        self.commands.append(command)
        completed = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True)
        return {
            "exit_code": completed.returncode,
            "output": completed.stdout,
            "success": completed.returncode == 0,
        }


def test_a_workdir_the_scan_could_not_read_proves_no_absence(tmp_path):
    """The executed probe: a real `find`, a directory that is not there.

    A settled job whose workdir vanished used to come back
    `complete=True, total=0` — every stage of the pipeline exits 0 and the END
    block prints its marker over an empty stream — and the harvest turned that
    into `gradle_no_test_reports_on_disk`, a proved-absence claim about a tree
    nothing ever read. The ofbiz case this vocabulary exists for is the
    opposite: a directory that WAS read and held nothing.
    """
    present = tmp_path / "proj" / "m" / "build" / "test-results" / "test"
    present.mkdir(parents=True)
    (present / "TEST-a.xml").write_bytes(_fixture("kafka-green-suite.xml"))
    shell = ShellContainer()

    read = _gradle_discover_reports(shell.execute_command, str(tmp_path / "proj"))
    unread = _gradle_discover_reports(shell.execute_command, str(tmp_path / "gone"))

    assert read == (((str(present / "TEST-a.xml")),), 1, True)
    assert unread.complete is False and unread.total == 0
    # And the harvest over that vanished tree declares nothing at all.
    harvest = gradle_test_harvest(
        shell.execute_command,
        working_directory=str(tmp_path / "gone"),
        delta={"new": [], "changed": []},
        test_dispatch=True,
    )
    assert harvest == type(harvest)()


# --- tier 1: suite totals from a bounded head ------------------------------


def _run_head_reader(paths, tmp_path):
    """Run the ACTUAL in-container reader, on the real interpreter."""
    script = _GRADLE_SUITE_HEAD_READER.replace("HEAD_BYTES", str(GRADLE_SUITE_HEAD_BYTES))
    payload = tmp_path / "input.json"
    payload.write_text(json.dumps([str(path) for path in paths]))
    completed = subprocess.run(
        [sys.executable, "-c", script, str(payload)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_tier_one_reads_a_137MB_report_total_out_of_its_first_4KB(tmp_path):
    """The whole point: the root `<testsuite>` tag is in the first line.

    `giant-head-4k.xml.head` is byte-for-byte the first 4,096 bytes of kafka's
    137,787,128-byte TaskAssignorConvergenceTest report. Reading that report
    whole is not slow, it is impossible against a 16 MB receipt budget — and it
    is also unnecessary, because the file states its own totals up front.
    """
    payload = _run_head_reader([FIXTURES / "giant-head-4k.xml.head"], tmp_path)

    assert payload["status"] == "complete"
    assert payload["suites"][0]["tests"] == 12
    assert payload["suites"][0]["failures"] == 0
    assert payload["suites"][0]["errors"] == 0
    assert payload["suites"][0]["skipped"] == 0
    assert (FIXTURES / "giant-head-4k.xml.head").stat().st_size == GRADLE_SUITE_HEAD_BYTES


def test_tier_one_reads_the_real_red_and_green_kafka_suites(tmp_path):
    payload = _run_head_reader(
        [FIXTURES / "kafka-red-suite.xml", FIXTURES / "kafka-green-suite.xml"], tmp_path
    )

    red, green = payload["suites"]
    assert (red["tests"], red["failures"], red["errors"], red["skipped"]) == (19, 1, 0, 0)
    assert (green["tests"], green["failures"], green["errors"], green["skipped"]) == (1, 0, 0, 0)


def test_a_head_with_no_parsable_suite_root_is_disclosed_never_guessed(tmp_path):
    """ "Unreadable" and "zero tests" are different facts about a report."""
    blank = tmp_path / "TEST-blank.xml"
    blank.write_text("<?xml version='1.0'?>\n<testsuites/>\n")

    payload = _run_head_reader([blank, tmp_path / "does-not-exist.xml"], tmp_path)

    # `<testsuites>` is a wrapper and declares nothing; a missing file declares
    # nothing. Neither entry carries a count, so neither can be summed.
    assert payload["suites"] == [
        {"path": str(blank)},
        {"path": str(tmp_path / "does-not-exist.xml")},
    ]


# --- tier 2: red-first identities under the existing tag bounds -------------


def test_red_bearing_reports_are_read_before_every_green_one():
    entries = [
        {"path": "/g1.xml", "tests": 500, "failures": 0, "errors": 0},
        {"path": "/red.xml", "tests": 19, "failures": 1, "errors": 0},
        {"path": "/g0.xml", "tests": 500, "failures": 0, "errors": 0},
        {"path": "/err.xml", "tests": 3, "failures": 0, "errors": 2},
    ]

    assert _gradle_red_first(entries) == ["/err.xml", "/red.xml", "/g0.xml", "/g1.xml"]


def test_identity_rows_come_from_tags_bound_to_the_digest_they_were_read_from():
    """The tag stream carries its report's digest, so it can be refused."""
    path = f"{ROOT}/clients/build/test-results/test/TEST-red.xml"
    body = _fixture("kafka-red-suite.xml").decode()
    stream = f"{REPORT_MARKER}{_digest('kafka-red-suite.xml')}  {path}\n{body}"

    rows = parse_report_tag_rows(stream, report_claims={path: _digest("kafka-red-suite.xml")})

    assert len(rows) == 19
    assert all(row["report_path"] == path for row in rows)
    failed = [row for row in rows if row["outcome"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["classname"].endswith("ConfigurationUtilsTest")
    assert failed[0]["reason"]
    assert [row["execution_ordinal"] for row in rows] == list(range(1, 20))


def test_tags_from_bytes_the_delta_does_not_claim_are_not_this_run_s_evidence():
    path = f"{ROOT}/clients/build/test-results/test/TEST-red.xml"
    body = _fixture("kafka-red-suite.xml").decode()
    stream = f"{REPORT_MARKER}{'0' * 64}  {path}\n{body}"

    # A report the delta claims at a DIFFERENT digest: the bytes on disk are
    # not the bytes this receipt is accountable for.
    assert parse_report_tag_rows(stream, report_claims={path: _digest("kafka-red-suite.xml")}) == []
    # And a report the delta does not name at all.
    assert parse_report_tag_rows(stream, report_claims={}) == []


def test_the_tag_read_is_bounded_and_never_asks_for_a_whole_report():
    command = report_tag_command("/w/TEST-giant.xml", tag_cap=400)

    assert "grep -oE" in command and "head -n 400" in command
    assert "sha256sum" in command
    assert "cat " not in command


# --- the whole harvest -----------------------------------------------------


def _kafka_harvest(container, *, delta):
    return gradle_test_harvest(
        container.execute_command,
        working_directory=ROOT,
        delta=delta,
        test_dispatch=True,
    )


def test_a_test_run_that_left_no_reports_states_absence_and_never_a_zero():
    """ofbiz-plugins ran its test task and wrote no XML at all."""
    container = FakeContainer(discovery=_discovery_output([], total=0))

    harvest = _kafka_harvest(container, delta={"new": [], "changed": []})

    assert harvest.suite_summaries is None and harvest.row_disclosure is None
    assert [entry["field"] for entry in harvest.omissions] == [
        "gradle_suite_summaries",
        "gradle_row_disclosure",
    ]
    assert all(entry["reasons"] == [GRADLE_NO_TEST_REPORTS] for entry in harvest.omissions)


def test_a_discovery_that_could_not_finish_states_nothing_at_all():
    """Unknown is an absent key, never a claim that the build produced nothing.

    An omission asserts something IS missing. A probe that went unanswered
    proves neither presence nor absence, and turning it into a declaration
    would make every flaky read look like a barren build.
    """
    container = FakeContainer(discovery=None)

    harvest = _kafka_harvest(container, delta={"new": [], "changed": []})

    assert harvest == type(harvest)()
    assert GRADLE_DISCOVERY_INCOMPLETE not in DECLARED_OMISSION_REASONS


def test_unreadable_suite_totals_are_an_omission_not_an_empty_summary():
    path = f"{ROOT}/clients/build/test-results/test/TEST-a.xml"
    container = FakeContainer(discovery=_discovery_output([path]), heads=None)

    harvest = _kafka_harvest(container, delta=_delta(("kafka-green-suite.xml", path)))

    assert harvest.suite_summaries is None
    assert {entry["reasons"][0] for entry in harvest.omissions} == {GRADLE_SUITE_TOTALS_UNREADABLE}


def test_a_dispatch_that_ran_no_test_task_harvests_and_states_nothing():
    """`assemble` leaving no test XML is a build, not missing evidence."""
    container = FakeContainer(discovery=_discovery_output([], total=0))

    harvest = gradle_test_harvest(
        container.execute_command,
        working_directory=ROOT,
        delta={"new": [], "changed": []},
        test_dispatch=gradle_test_action("assemble"),
    )

    assert harvest == type(harvest)()
    assert container.commands == []


def test_the_harvest_sums_every_claimed_report_and_samples_identities_red_first():
    """The kafka shape in miniature: two task dirs, one red among the greens."""
    red = f"{ROOT}/clients/build/test-results/test/TEST-red.xml"
    green = f"{ROOT}/clients/build/test-results/test/TEST-green.xml"
    distributed = f"{ROOT}/streams/build/test-results/distributedTest/TEST-green.xml"
    container = FakeContainer(
        discovery=_discovery_output([green, red, distributed]),
        heads=_head_output(
            [
                {"path": green, "tests": 1, "failures": 0, "errors": 0, "skipped": 0},
                {"path": red, "tests": 19, "failures": 1, "errors": 0, "skipped": 0},
                {"path": distributed, "tests": 1, "failures": 0, "errors": 0, "skipped": 0},
            ]
        ),
        tags="".join(
            f"{REPORT_MARKER}{_digest(name)}  {path}\n{_fixture(name).decode()}"
            for name, path in (
                ("kafka-red-suite.xml", red),
                ("kafka-green-suite.xml", green),
                ("kafka-green-suite.xml", distributed),
            )
        ),
    )

    harvest = _kafka_harvest(
        container,
        delta=_delta(
            ("kafka-red-suite.xml", red),
            ("kafka-green-suite.xml", green),
            ("kafka-green-suite.xml", distributed),
        ),
    )

    # Counts are complete and per (project, task dir) — geode's rule.
    assert harvest.suite_summaries["suites"] == [
        {
            "module": ":clients",
            "task": "test",
            "xml_files": 2,
            "tests": 20,
            "failures": 1,
            "errors": 0,
            "skipped": 0,
        },
        {
            "module": ":streams",
            "task": "distributedTest",
            "xml_files": 1,
            "tests": 1,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
        },
    ]
    assert "truncated" not in harvest.suite_summaries
    # The red report is read first, and its failure identity survives.
    assert container.commands[-1].index("TEST-red.xml") < container.commands[-1].index(
        "TEST-green.xml"
    )
    assert harvest.row_disclosure["red_rows_complete"] is True
    nodes = harvest.testcase_outcomes["nodes"]
    assert nodes[0]["status"] == "failed"
    assert "ConfigurationUtilsTest" in nodes[0]["node_id"]
    # Per-module executed witnesses, in `module_outcomes`' own grammar.
    assert harvest.module_tests_reported == {"clients": 20, "streams": 1}


# --- the claim binding: whose tests these are ------------------------------


def test_a_scoped_rerun_counts_only_what_it_wrote_over_the_reactors_leftovers():
    """The failure-repair loop, which is the ordinary case and not an exotic one.

    Dispatch 1 runs `test` across the reactor and leaves XML in every module.
    Dispatch 2 re-runs `:clients:test` alone: it rewrites clients' report and
    touches nothing else, so `report_delta` claims clients' bytes and puts
    every leftover in no bucket — "not this invocation's evidence" is a
    decision the delta has already made, on hashes, before the harvest looks.

    Discovery still finds them all, because it is a tree scan. Summing what it
    finds is how a scoped re-run comes to state 9,019 tests it did not run, and
    how a stale module's failure withdraws a green run's red-completeness. The
    scan finds; the delta decides whose.
    """
    mine = f"{ROOT}/clients/build/test-results/test/TEST-green.xml"
    stale_red = f"{ROOT}/streams/build/test-results/test/TEST-red.xml"
    stale_green = f"{ROOT}/metadata/build/test-results/test/TEST-green.xml"
    container = FakeContainer(
        discovery=_discovery_output([mine, stale_green, stale_red]),
        heads={
            mine: {"tests": 1, "failures": 0, "errors": 0, "skipped": 0},
            # 9,019 tests across three modules is what a receipt claiming the
            # tree states; 9,018 of them are dispatch 1's, and two of dispatch
            # 1's failures are the reds that withdraw this run's completeness.
            stale_green: {"tests": 8999, "failures": 0, "errors": 0, "skipped": 0},
            stale_red: {"tests": 19, "failures": 2, "errors": 0, "skipped": 0},
        },
        tags=(
            f"{REPORT_MARKER}{_digest('kafka-green-suite.xml')}  {mine}\n"
            f"{_fixture('kafka-green-suite.xml').decode()}"
        ),
    )

    harvest = _kafka_harvest(container, delta=_delta(("kafka-green-suite.xml", mine)))

    # One module in the totals, and it is the one this dispatch ran.
    assert harvest.suite_summaries["suites"] == [
        {
            "module": ":clients",
            "task": "test",
            "xml_files": 1,
            "tests": 1,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
        }
    ]
    # §14.2: a module gains an executed witness only where this dispatch wrote
    # the report that witnesses it.
    assert harvest.module_tests_reported == {"clients": 1}
    # The stale failure is not this run's, so it neither counts as red nor
    # withdraws the claim that every red in these summaries is in the rows.
    assert harvest.row_disclosure["red_rows_complete"] is True
    assert all(node["status"] != "failed" for node in harvest.testcase_outcomes["nodes"])
    # And the leftovers were never even read: the binding is upstream of both
    # tiers, not a filter applied to their output.
    assert container.head_input == [mine]
    assert stale_red not in container.commands[-1]


def test_a_tree_of_nothing_but_leftovers_states_absence_not_another_run_s_totals():
    """Reports on disk, none of them this dispatch's.

    Every report is byte-identical to what an earlier attempt left and no cache
    hit vouched for any of it. Stating the tree's totals would hand this
    receipt tests it did not run; stating a zero would read as a clean project
    with no tests. It states the one thing it proved — that it wrote none.
    """
    stale = f"{ROOT}/clients/build/test-results/test/TEST-green.xml"
    container = FakeContainer(
        discovery=_discovery_output([stale]),
        heads={stale: {"tests": 3, "failures": 0, "errors": 0, "skipped": 0}},
    )

    harvest = _kafka_harvest(container, delta={"new": [], "changed": []})

    assert harvest.suite_summaries is None and harvest.row_disclosure is None
    assert harvest.testcase_outcomes is None
    assert harvest.module_tests_reported == {}
    assert [entry["field"] for entry in harvest.omissions] == [
        "gradle_suite_summaries",
        "gradle_row_disclosure",
    ]
    assert all(entry["reasons"] == [GRADLE_NO_CLAIMED_TEST_REPORTS] for entry in harvest.omissions)
    # Absence-of-mine and absence-on-disk are different facts and never share
    # a reason: the ofbiz claim stays available for the tree that is empty.
    assert GRADLE_NO_CLAIMED_TEST_REPORTS != GRADLE_NO_TEST_REPORTS


def test_a_tag_read_that_never_landed_keeps_the_totals_and_discloses_no_sample():
    """A disclosure describes a sample. With no sample there is nothing to say.

    Only the tag round trip failed here — the totals came from a different read
    and still stand. Attaching a disclosure anyway would state
    `rows_source: gradle_xml` and a file-by-file drop count beside a receipt
    carrying no such rows, and `record_invocation` binds its bounded list to
    the disclosure precisely so the two cannot describe different measurements.
    """
    path = f"{ROOT}/clients/build/test-results/test/TEST-red.xml"
    container = FakeContainer(
        discovery=_discovery_output([path]),
        heads=_head_output([{"path": path, "tests": 19, "failures": 1, "errors": 0, "skipped": 0}]),
        tags=None,
    )

    harvest = _kafka_harvest(container, delta=_delta(("kafka-red-suite.xml", path)))

    assert harvest.suite_summaries["suites"][0]["tests"] == 19
    assert harvest.module_tests_reported == {"clients": 19}
    assert harvest.row_disclosure is None
    assert harvest.testcase_outcomes is None
    assert harvest.omissions == (
        {"field": "gradle_row_disclosure", "reasons": [GRADLE_ROW_SAMPLE_UNREADABLE]},
    )


def test_the_file_bound_states_the_reports_the_sample_never_spoke_for():
    """kafka: 1,176 reports, a 50-file tag bound. The 1,126 are a stated loss."""
    paths = [
        f"{ROOT}/m{index}/build/test-results/test/TEST-{index}.xml"
        for index in range(TESTCASE_FILE_CAP + 5)
    ]
    container = FakeContainer(
        discovery=_discovery_output(paths),
        heads=_head_output(
            [{"path": path, "tests": 1, "failures": 0, "errors": 0, "skipped": 0} for path in paths]
        ),
        tags="".join(
            f"{REPORT_MARKER}{_digest('kafka-green-suite.xml')}  {path}\n"
            f"{_fixture('kafka-green-suite.xml').decode()}"
            for path in paths[:TESTCASE_FILE_CAP]
        ),
    )

    harvest = _kafka_harvest(
        container,
        delta=_delta(*(("kafka-green-suite.xml", path) for path in paths)),
    )

    assert harvest.suite_summaries["suites"][0]["xml_files"] == 1
    assert len(harvest.suite_summaries["suites"]) == TESTCASE_FILE_CAP + 5
    truncation = harvest.row_disclosure["rows_truncated"]
    # Five reports were never read at all; the disclosure counts them as
    # reports this sample does not speak for.
    assert truncation["dropped_files"] == 5
    assert truncation["dropped_green"] == 0
    assert "dropped_red" not in truncation


def test_a_discovery_bound_that_fired_withdraws_the_red_completeness_claim():
    """A red can be hiding in a report THIS RUN WROTE that nobody read.

    The bound is counted in the unit that matters: reports this invocation
    claims and the capped listing never named. Leftovers the bound also left
    out cost this receipt nothing — they were never its evidence — so a scoped
    re-run over a huge tree does not forfeit its red claim for them.
    """
    path = f"{ROOT}/clients/build/test-results/test/TEST-green.xml"
    unlisted = [f"{ROOT}/clients/build/test-results/test/TEST-unlisted-{n}.xml" for n in range(2)]
    container = FakeContainer(
        discovery=_discovery_output([path], total=GRADLE_XML_FILE_CAP + 3),
        heads=_head_output([{"path": path, "tests": 1, "failures": 0, "errors": 0, "skipped": 0}]),
        tags=(
            f"{REPORT_MARKER}{_digest('kafka-green-suite.xml')}  {path}\n"
            f"{_fixture('kafka-green-suite.xml').decode()}"
        ),
    )

    harvest = _kafka_harvest(
        container,
        delta=_delta(*(("kafka-green-suite.xml", claimed) for claimed in (path, *unlisted))),
    )

    assert harvest.suite_summaries["unsummarized_files"] == len(unlisted)
    assert harvest.row_disclosure["red_rows_complete"] is False


# --- module_outcomes gains an executed witness ------------------------------


def test_module_outcomes_gains_a_count_only_for_modules_the_build_named():
    """Enrich, never extend: this list is the coverage denominator."""
    outcomes = _gradle_module_outcomes(
        "> Task :clients:test\n> Task :streams:test FAILED\n> Task :core:compileJava\n"
    )

    enriched = _gradle_module_outcomes_with_counts(
        outcomes, {"clients": 20, "streams": 3, "metadata": 900}
    )

    assert enriched == [
        {"module": "clients", "status": "attempted", "tests_reported": 20},
        {"module": "streams", "status": "failure", "tests_reported": 3},
        {"module": "core", "status": "attempted"},
    ]


@pytest.mark.parametrize("count", [0, 27219])
def test_the_receipt_schema_carries_a_module_executed_count(count):
    receipt = _receipt_with_modules(
        [{"module": "clients", "status": "attempted", "tests_reported": count}]
    )

    assert validate_receipt_v2(receipt)["module_outcomes"][0]["tests_reported"] == count


@pytest.mark.parametrize("bad", [True, -1, "20", None])
def test_a_module_count_that_is_not_a_count_is_refused(bad):
    receipt = _receipt_with_modules(
        [{"module": "clients", "status": "attempted", "tests_reported": bad}]
    )

    with pytest.raises(ValueError, match="tests_reported|module_outcomes"):
        validate_receipt_v2(receipt)


def _receipt_with_modules(modules):
    return {
        "schema_version": 3,
        "receipt_id": "inv-gradle-test-0001",
        "run_id": "run-pytest",
        "tool": "gradle",
        "requested_action": "test",
        "effective_action": "test",
        "argv": "./gradlew test",
        "working_directory": ROOT,
        "actual_cwd": ROOT,
        "exit_code": 0,
        "outcome": "completed",
        "report_delta": {"new": [], "changed": []},
        "module_outcomes": modules,
    }


# --- a runner-declared omission is receipt evidence, on a closed vocabulary --


def _built(**overrides):
    return build_receipt(
        receipt_id="inv-gradle-1-0007",
        run_id="run-pytest",
        tool="gradle",
        requested_action="test",
        effective_action="test",
        argv="./gradlew test",
        working_directory=ROOT,
        exit_code=0,
        before={},
        after={},
        **overrides,
    )


def test_a_test_run_with_no_reports_declares_the_omission_on_the_receipt():
    """The ofbiz case, end to end: absence reaches the receipt as a statement."""
    receipt = _built(
        declared_omissions=[
            {"field": "gradle_suite_summaries", "reasons": [GRADLE_NO_TEST_REPORTS]},
            {"field": "gradle_row_disclosure", "reasons": [GRADLE_NO_TEST_REPORTS]},
        ]
    )

    assert receipt["evidence_omissions"] == [
        {
            "field": "gradle_row_disclosure",
            "status": "unavailable",
            "reasons": [GRADLE_NO_TEST_REPORTS],
        },
        {
            "field": "gradle_suite_summaries",
            "status": "unavailable",
            "reasons": [GRADLE_NO_TEST_REPORTS],
        },
    ]
    assert validate_receipt_v2(receipt)["evidence_omissions"] == receipt["evidence_omissions"]
    assert "gradle_suite_summaries" not in receipt


@pytest.mark.parametrize(
    "declared",
    [
        # Prose. A runner names which measurement it could not make; it never
        # writes a sentence into the receipt.
        {"field": "gradle_suite_summaries", "reasons": ["the container was busy"]},
        # A field that is not an omittable observability field.
        {"field": "report_delta", "reasons": [GRADLE_NO_TEST_REPORTS]},
        # A reason list with nothing in it states nothing.
        {"field": "gradle_suite_summaries", "reasons": []},
    ],
)
def test_a_declared_omission_outside_the_closed_vocabulary_never_lands(declared):
    assert "evidence_omissions" not in _built(declared_omissions=[declared])


def test_a_receipt_may_state_its_totals_and_name_the_row_sample_it_lacks():
    """The two sections fail independently, so they are omitted independently.

    A tag read that did not land takes the identity sample with it and leaves
    every count standing. The receipt that results carries the totals, names
    the one measurement it could not make, and carries no disclosure — so no
    reader can be told about drops from a sample that is not there.
    """
    summaries = {
        "suites": [
            {
                "module": ":clients",
                "task": "test",
                "xml_files": 1,
                "tests": 19,
                "failures": 1,
                "errors": 0,
                "skipped": 0,
            }
        ]
    }

    receipt = _built(
        gradle_suite_summaries=summaries,
        declared_omissions=[
            {"field": "gradle_row_disclosure", "reasons": [GRADLE_ROW_SAMPLE_UNREADABLE]}
        ],
    )

    assert receipt["gradle_suite_summaries"] == summaries
    assert "gradle_row_disclosure" not in receipt
    assert receipt["evidence_omissions"] == [
        {
            "field": "gradle_row_disclosure",
            "status": "unavailable",
            "reasons": [GRADLE_ROW_SAMPLE_UNREADABLE],
        }
    ]
    assert validate_receipt_v2(receipt)["evidence_omissions"] == receipt["evidence_omissions"]


def test_a_declared_omission_cannot_contradict_a_section_the_receipt_carries():
    summaries = {
        "suites": [
            {
                "module": ":clients",
                "task": "test",
                "xml_files": 1,
                "tests": 1,
                "failures": 0,
                "errors": 0,
                "skipped": 0,
            }
        ]
    }

    receipt = _built(
        gradle_suite_summaries=summaries,
        declared_omissions=[
            {"field": "gradle_suite_summaries", "reasons": [GRADLE_SUITE_TOTALS_UNREADABLE]}
        ],
    )

    assert receipt["gradle_suite_summaries"] == summaries
    assert "evidence_omissions" not in receipt


# --- the synchronous runner carries the harvest onto its own receipt --------


class HarvestingReceiptOrchestrator(ReceiptOrchestrator):
    """`ReceiptOrchestrator` that also answers the three harvest round trips."""

    def __init__(self, *, report, **kwargs):
        super().__init__(build_system="gradle", **kwargs)
        self.report = report

    def execute_command(self, command, workdir=None, timeout=None, **kwargs):
        if COUNT_MARKER in command:
            return ok(f"{self.report}\n{COUNT_MARKER}1 0\n")
        if "ATTRIBUTE = re.compile" in command:
            return ok(
                json.dumps(
                    {
                        "status": "complete",
                        "suites": [
                            {
                                "path": self.report,
                                "tests": 19,
                                "failures": 1,
                                "errors": 0,
                                "skipped": 0,
                            }
                        ],
                    }
                )
            )
        if REPORT_MARKER in command:
            return ok(
                f"{REPORT_MARKER}{_digest('kafka-red-suite.xml')}  {self.report}\n"
                f"{_fixture('kafka-red-suite.xml').decode()}"
            )
        return super().execute_command(command, workdir=workdir, timeout=timeout, **kwargs)


def test_a_synchronous_gradle_test_run_states_its_totals_and_its_red(
    facade_contract_authority,
):
    """What the measured kafka receipt could not say, said.

    19 tests and one failure from one module — the same shape as the run that
    reported `module_outcomes` and nothing else across 27,219 tests.
    """
    report = "/workspace/proj/clients/build/test-results/test/TEST-red.xml"
    digest = _digest("kafka-red-suite.xml")
    orchestrator = HarvestingReceiptOrchestrator(
        report=report,
        snapshots=["", f"{digest}  {report}"],
        monitored_result={
            "output": "> Task :clients:test\nBUILD SUCCESSFUL in 3s",
            "exit_code": 0,
        },
    )

    with argv_contract_authority(
        executor="gradle", action="test", expected_argv="--build-cache test"
    ):
        GradleTool(orchestrator).execute(tasks="test", working_directory="/workspace/proj")

    (receipt,) = receipts_written(orchestrator.receipt_commands)
    assert receipt["gradle_suite_summaries"] == {
        "suites": [
            {
                "module": ":clients",
                "task": "test",
                "xml_files": 1,
                "tests": 19,
                "failures": 1,
                "errors": 0,
                "skipped": 0,
            }
        ]
    }
    assert receipt["module_outcomes"] == [
        {"module": "clients", "status": "attempted", "tests_reported": 19}
    ]
    # The failure identity survives the bounded sample and sorts first.
    assert receipt["testcase_outcomes"]["nodes"][0]["status"] == "failed"
    assert receipt["gradle_row_disclosure"]["red_rows_complete"] is True
    assert "evidence_omissions" not in receipt
    # The report's own bytes never crossed into anything the model reads.
    assert not any("cat " in command for command in orchestrator.receipt_commands)

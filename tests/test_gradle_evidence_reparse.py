# tests/test_gradle_evidence_reparse.py
"""Plan acceptance — the three measured trees, re-parsed by the new path.

`docs/superpowers/plans/2026-09-01-gradle-receipt-rows-r2.md` closes on one
clause: "the same three container evidence trees re-parsed through the new path
produce the same aggregate numbers the evidence report pinned (27,219/8/32,
10,435/0/22, 179/0/0)". Those numbers were measured OUTSIDE this engine — the
2026-08-30 study extracted `build/test-results` from the stopped
`sag-d2r6-{kafka,geode,spark-kubernetes-operator}` containers and counted them
with its own tooling. Every other test in this branch is the engine checked
against fixtures the engine's own authors wrote; this one is the engine checked
against an independent count of real bytes.

What runs here is the production path and only it: `snapshot_reports` takes the
hash bracket, `report_delta` names the claim set, and `gradle_test_harvest`
sums the reports through the real in-container reader programs on a real shell.
Nothing in this file parses XML. If a number below moves, the engine's reading
of a Gradle run moved with it.

The evidence is 220 MB of container output and is not in the repository — the
tars live under the repo's own gitignored `logs/`, which is where the archived
run evidence goes. Absent, this file skips; present, it is the acceptance.
"""

import base64
import json
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from hashlib import sha256
from pathlib import Path

import pytest

from sag.agent.invocation_receipts import (
    RECEIPT_MAX_CANONICAL_BYTES,
    ReportSnapshot,
    build_receipt,
    report_delta,
    snapshot_reports,
    validate_receipt_v2,
)
from sag.agent.receipt_suite_totals import receipt_suite_totals
from sag.tools.internal.gradle_tool import (
    _GRADLE_HEAD_INPUT_DIR,
    _GRADLE_REPORT_COUNT_MARKER,
    _gradle_report_identity,
    gradle_test_harvest,
)
from sag.utils.container_io import MAX_CONTAINER_COMMAND_CHARS

# The archive the study left, addressed the way the repo addresses its own run
# evidence: `<repo>/logs/<archive>`. A worktree keeps its own gitignored
# `logs/`, so the search walks up until it finds the archive itself rather than
# stopping at the first directory that merely has the name.
EVIDENCE_ARCHIVE = Path("logs") / "gradle-evidence-20260830"
CHECKSUMS = "SHA256SUMS"

# `docs/superpowers/reports/gradle-evidence-20260830.md`, the study's own table:
#
# | | kafka | geode | spark-kubernetes-operator |
# | XML files | 1,176 (19 modules) | 1,167 (27 modules) | 40 (3 modules) |
# | executed / red / skipped | 27,219 / 8 / 32 | 10,435 / 0 / 22 | 179 / 0 / 0 |
#
# `root` is the build root inside the archive — the directory a dispatch would
# have run in, which is what makes `:clients` and `:extensions:geode-modules`
# the project paths the receipt states.
PINNED = {
    "kafka": {
        "tar": "kafka-d2r6-container/test-results.tar",
        "root": ".",
        "xml_files": 1176,
        "modules": 19,
        "executed": 27_219,
        "red": 8,
        "skipped": 32,
    },
    "geode": {
        "tar": "geode-d2r6-container/test-results.tar",
        "root": "geode",
        "xml_files": 1167,
        "modules": 27,
        "executed": 10_435,
        "red": 0,
        "skipped": 22,
    },
    "spark-kubernetes-operator": {
        "tar": "spark-k8s-d2r6-container/test-results.tar",
        "root": "spark-kubernetes-operator",
        "xml_files": 40,
        "modules": 3,
        "executed": 179,
        "red": 0,
        "skipped": 0,
    },
}

# The eight kafka failures, by the identity each report gave them. The study
# named the three modules they fell in (clients, metadata, streams); these are
# the rows themselves, which only a parse of the real XML can produce.
KAFKA_RED_NODES = (
    "org.apache.kafka.common.security.oauthbearer.internals.secured."
    "ConfigurationUtilsTest#testFileUnreadable()",
    "org.apache.kafka.metadata.properties.MetaPropertiesEnsembleTest"
    "#testMetaPropertiesEnsembleLoadError()",
    "org.apache.kafka.metadata.storage.FormatterTest#testFormatterFailsOnUnwritableDirectory()",
    "org.apache.kafka.streams.processor.internals.GlobalStateManagerImplTest"
    "#shouldLogWarningMessageWhenIOExceptionInCheckPoint()",
    "org.apache.kafka.streams.state.internals.RocksDBMigratingSessionStoreWithHeadersTest"
    "#shouldThrowProcessorStateExceptionOnOpeningReadOnlyDir()",
    "org.apache.kafka.streams.state.internals.RocksDBStoreTest"
    "#shouldThrowProcessorStateExceptionOnOpeningReadOnlyDir()",
    "org.apache.kafka.streams.state.internals.RocksDBTimestampedStoreTest"
    "#shouldThrowProcessorStateExceptionOnOpeningReadOnlyDir()",
    "org.apache.kafka.streams.state.internals.RocksDBTimestampedStoreWithHeadersTest"
    "#shouldThrowProcessorStateExceptionOnOpeningReadOnlyDir()",
)
KAFKA_RED_MODULES = {":clients": 1, ":metadata": 2, ":streams": 5}

RUN_ID = "run-gradle-evidence-reparse"
TARGET_SHA = "26b251a4" + "0" * 32
CONTRACT = {
    "contract_id": "ic-0123456789ab",
    "contract_hash": "a" * 64,
    "execution_binding": "argv_v1",
    "compliance": "exact",
}


def _evidence_archive():
    """The archive directory, wherever this checkout keeps its run evidence."""

    for parent in Path(__file__).resolve().parents:
        candidate = parent / EVIDENCE_ARCHIVE
        if all((candidate / pin["tar"]).is_file() for pin in PINNED.values()):
            return candidate
    return None


ARCHIVE = _evidence_archive()
# `sha256sum` and `find` are the container's, and the bracket and the bounded
# tag read are written in them. A host without them cannot run this path at
# all, which is a skip and never a silent partial answer.
MISSING_TOOL = next((name for name in ("sha256sum", "find") if not shutil.which(name)), None)

pytestmark = [
    pytest.mark.skipif(
        ARCHIVE is None,
        reason=f"no gradle evidence archive under {EVIDENCE_ARCHIVE} in any parent",
    ),
    pytest.mark.skipif(
        MISSING_TOOL is not None,
        reason=f"the harvest's own {MISSING_TOOL or 'shell tooling'} is not on this host",
    ),
]


class HostShell:
    """The harvest's own commands, run by a real shell over the real archive.

    Two host adaptations, both about WHERE and HOW the shell runs rather than
    what it runs: the reader's input file is staged under the test's own
    directory instead of the container's `/workspace`, and BSD `base64` is
    given the input flag its GNU counterpart does not need. Every program that
    reads a report — the bracket's `find`/`sha256sum`, the suite-head reader,
    the bounded tag read — is the production string, unmodified.
    """

    def __init__(self, staging: Path):
        self.staging = str(staging)
        self.commands: list[str] = []

    def __call__(self, command, **kwargs):
        return self.execute_command(command, **kwargs)

    def execute_command(self, command, **kwargs):
        del kwargs
        self.commands.append(command)
        adapted = command.replace(_GRADLE_HEAD_INPUT_DIR, self.staging)
        if sys.platform != "linux":
            adapted = adapted.replace("base64 --decode ", "base64 --decode -i ")
        completed = subprocess.run(["/bin/sh", "-c", adapted], capture_output=True, text=True)
        return {
            "exit_code": completed.returncode,
            "output": completed.stdout,
            "success": completed.returncode == 0,
        }

    def execute_control_command(self, command, **kwargs):
        return self.execute_command(command, **kwargs)


def _verify_archive_digests(archive: Path):
    """The tars are the study's own bytes, or these numbers are about nothing."""

    listed = {}
    for line in (archive / CHECKSUMS).read_text().splitlines():
        digest, _, name = line.strip().partition(" ")
        if digest and name.strip():
            listed[name.strip()] = digest
    for pin in PINNED.values():
        path = archive / pin["tar"]
        assert listed.get(pin["tar"]), f"{pin['tar']} is not named in {CHECKSUMS}"
        assert (
            sha256(path.read_bytes()).hexdigest() == listed[pin["tar"]]
        ), f"{pin['tar']} is not the archive the study measured"


def _harvest_tree(name: str, workspace: Path):
    """Extract one tree and read it exactly as a terminal dispatch would.

    The bracket is taken with an empty BEFORE, which is what a dispatch that
    wrote these reports had: every report in the tree is this invocation's, and
    the delta says so with the digest each was read at.
    """

    pin = PINNED[name]
    tree = workspace / name
    tree.mkdir(parents=True)
    with tarfile.open(ARCHIVE / pin["tar"]) as archive:
        archive.extractall(tree, filter="data")
    root = (tree if pin["root"] == "." else tree / pin["root"]).resolve()
    staging = workspace / f"{name}-staging"
    staging.mkdir()
    shell = HostShell(staging)

    after = snapshot_reports(shell, [str(root)])
    delta = report_delta(ReportSnapshot({}, complete=True), after)
    harvest = gradle_test_harvest(
        shell,
        working_directory=str(root),
        delta=delta,
        test_disposition="planned",
    )
    return {"pin": pin, "root": str(root), "after": dict(after), "harvest": harvest, "shell": shell}


@pytest.fixture(scope="module")
def reparsed():
    """Every tree, extracted and harvested once for the whole module."""

    with tempfile.TemporaryDirectory(prefix="gradle-evidence-") as workspace:
        _verify_archive_digests(ARCHIVE)
        yield {name: _harvest_tree(name, Path(workspace)) for name in PINNED}


def _totals(harvest):
    suites = harvest.suite_summaries["suites"]
    return {
        field: sum(suite[field] for suite in suites)
        for field in ("xml_files", "tests", "failures", "errors", "skipped")
    }


# --- the acceptance clause --------------------------------------------------


@pytest.mark.parametrize("name", sorted(PINNED))
def test_the_measured_tree_reparses_to_the_aggregate_the_study_pinned(reparsed, name):
    """The plan's closing clause, one tree at a time.

    Executed, red and skipped are summed from what each report's own
    `<testsuite>` root declares — the tier the 2026-08-26 session reported as
    `unavailable` for all 27,219 of them. `xml_files` and the module count come
    up with them, so a run that summed the right total out of the wrong file
    set could not pass this either.
    """
    read = reparsed[name]
    pin, harvest = read["pin"], read["harvest"]
    totals = _totals(harvest)

    assert harvest.omissions == ()
    assert totals["xml_files"] == pin["xml_files"]
    assert len(harvest.suite_summaries["suites"]) == pin["modules"]
    assert totals["tests"] == pin["executed"]
    assert totals["failures"] + totals["errors"] == pin["red"]
    assert totals["skipped"] == pin["skipped"]


@pytest.mark.parametrize("name", sorted(PINNED))
def test_the_claim_set_is_the_bracket_s_and_every_claim_was_summed(reparsed, name):
    """No bound fired, so these totals speak for the whole tree.

    A partial total that happened to match a pinned number would be a
    coincidence, not agreement. Each of the four ways the section can be less
    than the run — a truncated read, an unreadable suite, an unsummed claim, a
    post-snapshot rewrite — is absent here, and the file count the bracket
    hashed is the file count the totals sum.
    """
    read = reparsed[name]
    summaries = read["harvest"].suite_summaries

    assert set(summaries) == {"suites"}
    assert len(read["after"]) == read["pin"]["xml_files"]
    # Gradle's own internal result store sits beside every report directory and
    # is not evidence; nothing under it entered the claim set.
    assert not any("/binary/" in path for path in read["after"])
    assert read["harvest"].row_disclosure["red_rows_complete"] is True


def test_the_kafka_reds_are_named_not_merely_counted(reparsed):
    """Eight failures among 27,219 executions, and the sample holds all eight.

    This is the retention law measured against real evidence rather than a
    generated shape: the bounded sample is 50 nodes wide, the reds are 8 of
    27,219, and every one of them is in it — by name, with the assertion the
    report recorded, in the module the study found it in.
    """
    read = reparsed["kafka"]
    nodes = read["harvest"].testcase_outcomes["nodes"]
    reds = [node for node in nodes if node["status"] in ("failed", "error")]

    assert tuple(node["node_id"] for node in reds) == KAFKA_RED_NODES
    assert all(node["reason"] for node in reds)
    # The sample is a sample and says so; the totals beside it are not.
    assert read["harvest"].testcase_outcomes["truncated"] is True
    assert {
        suite["module"]: suite["failures"]
        for suite in read["harvest"].suite_summaries["suites"]
        if suite["failures"] or suite["errors"]
    } == KAFKA_RED_MODULES


def test_the_kafka_totals_survive_into_a_receipt_a_consumer_reads(reparsed):
    """The last hop: 27,219 out of the archive and into the live reading.

    The harvest's own sections are put through the production builder and its
    validators — the conservation rules, the module-witness sum, the
    disclosure arithmetic — and then read back by the consumer the metrics
    surface and the certificate adapter share. What comes out is the number the
    2026-08-26 session had on disk and could not state.
    """
    read = reparsed["kafka"]
    harvest = read["harvest"]
    receipt = build_receipt(
        receipt_id="inv-gradle-test-0001",
        run_id=RUN_ID,
        tool="gradle",
        requested_action="test",
        effective_action="test",
        argv="./gradlew --build-cache test",
        working_directory=read["root"],
        exit_code=0,
        before={},
        after=read["after"],
        target_sha=TARGET_SHA,
        domain_id="kafka",
        gradle_suite_summaries=harvest.suite_summaries,
        gradle_row_disclosure=harvest.row_disclosure,
        testcase_outcomes=harvest.testcase_outcomes,
        module_outcomes=[
            {"module": module, "status": "attempted", "tests_reported": total}
            for module, total in sorted(harvest.module_tests_reported.items())
        ],
        **CONTRACT,
    )

    assert validate_receipt_v2(receipt, expected_id=receipt["receipt_id"]) == receipt
    pin = PINNED["kafka"]
    totals = receipt_suite_totals(receipt)
    assert totals is not None
    # Complete claims, no bound disclosed: this receipt's counts speak for every
    # execution the archive holds, not for a floor under them.
    assert totals.complete_claims is True
    assert totals.disclosed_bounds == ()
    assert (totals.tests, totals.failed, totals.errors, totals.skipped) == (
        pin["executed"],
        pin["red"],
        0,
        pin["skipped"],
    )
    assert totals.passed == pin["executed"] - pin["red"] - pin["skipped"]


def test_no_command_the_reparse_issued_shipped_a_report_whole(reparsed):
    """The trust boundary, checked against the largest report ever measured.

    kafka's `TaskAssignorConvergenceTest` report is 137.8 MB. Every command
    that named a report here is one of the three bounded shapes — the bracket,
    the discovery glob, the per-report tag read — and the two reader programs
    named no report at all, because their path lists travelled as a file.
    """
    read = reparsed["kafka"]
    naming = [command for command in read["shell"].commands if "/build/test-results/" in command]

    assert naming
    for command in naming:
        bracket = "-exec sha256sum" in command
        bounded_tags = "grep -oE" in command and "head -n" in command
        assert bracket or bounded_tags, command
        assert "cat " not in command
    assert not any("python3 -c" in command for command in naming)
    # And the tree was never listed at all: the delta had already proved whose
    # these reports are, so no discovery scan was issued for any of them.
    assert not any(_GRADLE_REPORT_COUNT_MARKER in command for command in read["shell"].commands)

    # The report that made the whole design necessary: one file larger than the
    # entire receipt budget, whose counts are in the totals all the same,
    # because a `<testsuite>` root states them in the first 4 KB.
    largest = max(read["after"], key=lambda path: Path(path).stat().st_size)
    module, _ = _gradle_report_identity(largest, read["root"])
    assert Path(largest).stat().st_size > RECEIPT_MAX_CANONICAL_BYTES
    assert module in {suite["module"] for suite in read["harvest"].suite_summaries["suites"]}


def test_the_reader_input_travelled_as_a_file_never_as_an_argument(reparsed):
    """1,176 paths against ARG_MAX: the list is written, then named once.

    The head read's command carries ONE path — its input file — and the claim
    set reaches the reader inside it. Reconstructed from the chunks the write
    actually sent, that file holds every claimed report and nothing else.
    """
    read = reparsed["kafka"]
    (head_read,) = [
        command for command in read["shell"].commands if "ATTRIBUTE = re.compile" in command
    ]
    staged = shlex.split(head_read)[-1]
    encoded = "".join(
        shlex.split(command)[2]
        for command in read["shell"].commands
        if command.startswith("printf '%s' '") and f"{staged}.b64." in command
    )

    assert staged.startswith(_GRADLE_HEAD_INPUT_DIR) and staged.endswith(".json")
    assert len(head_read) < MAX_CONTAINER_COMMAND_CHARS
    assert encoded, "the path list must reach the reader through a written file"
    assert json.loads(base64.b64decode(encoded).decode("utf-8")) == sorted(read["after"])

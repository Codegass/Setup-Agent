# tests/test_gradle_receipt_rows_e2e.py
"""Plan — the whole chain, from a Gradle run's own disk to the numbers a report states.

Tasks 1 and 2 gave a Gradle receipt somewhere to put test evidence and taught
the dispatch to look. Neither of them proves the thing the feature exists for:
that the evidence ARRIVES — that the counts a run earned reach the consumers
which decide what the run is worth, through consumers nobody edited to let it
in.

The measured failure this closes (`docs/superpowers/reports/gradle-evidence-
20260830.md`, the 2026-08-26 kafka session): 1,176 report files and 27,219
executed tests on disk, and a metrics surface whose `receipt_executions` read

    {"executed": null, ..., "reason": "current receipt testcase rows were unavailable"}

That exact string is the assertion these tests are built around. It is what
`report_metrics` says when a current receipt exists and carries no usable
sealed row envelope, and it is what a Gradle run said about every test it ran.

The reactor here is real: the committed kafka fixtures laid out in a real
Gradle report tree and MOUNTED at the `/workspace` paths a container would
have (the receipt's own manifest refuses any other root, and a receipt whose
paths were fictions could only ever be answered by a stub — a stub cannot
prove a parse). `GradleTool` drives it through the real transports: the real
settings.gradle parse, the real hash bracket, and the real in-container row
parser and suite-head reader run on the real interpreter over the real fixture
bytes. Only the dispatch itself and the receipt's durable write are doubles,
because this is about what a run REPORTS, not about invoking Gradle.

Downstream is imported and called, never reimplemented: `validate_receipt_v2`,
`receipt_record_scope`, `validate_testcase_execution_row`,
`aggregate_testcase_execution_rows` and `assemble_report_metrics` are the
production article. If one of them had needed a Gradle-shaped exception to
ingest this, that exception would have had to be written here — the last test
in this file is the standing proof that none was.
"""

import json
import re
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest
from test_invocation_receipts import (
    ReceiptOrchestrator,
    argv_contract_authority,
    receipts_written,
)

from sag.agent.invocation_receipts import (
    RECEIPT_DIR,
    RECEIPT_MAX_CANONICAL_BYTES,
    RECEIPT_SCHEMA_VERSION,
    ROW_SECTION_BUDGET_SHARE,
    ROW_SECTION_MAX_CANONICAL_BYTES,
    SEALED_ROW_MAX_CANONICAL_BYTES,
    TESTCASE_OUTCOME_CAP,
    TESTCASE_ROWS_SOURCE,
    TESTCASE_TAG_PATTERN,
    build_receipt,
    receipt_record_scope,
    validate_receipt_v2,
)
from sag.agent.receipt_test_rows import (
    DELTA_TESTCASE_ROW_CAP,
    _row_parser_program,
    aggregate_testcase_execution_rows,
)
from sag.agent.receipt_test_rows import testcase_execution_id as execution_id_of
from sag.agent.receipt_test_rows import (
    validate_testcase_execution_row,
)
from sag.tools.internal.gradle_tool import (
    _GRADLE_SUITE_HEAD_READER,
    GRADLE_SUITE_HEAD_BYTES,
    GradleTool,
)
from sag.tools.report_metrics import assemble_report_metrics

FIXTURES = Path(__file__).parent / "fixtures" / "gradle_receipts"
COUNT_MARKER = "###sag-gradle-xml-count###"
REPORT_MARKER = "###sag-report###"
# The kafka d2r6 pin the evidence study measured against.
TARGET_SHA = "26b251a4" + "0" * 32
# The measured red: one of the eight root-user permission-class failures the
# study found, in the module it was found in.
RED_NODE = (
    "org.apache.kafka.common.security.oauthbearer.internals.secured."
    "ConfigurationUtilsTest#testFileUnreadable()"
)
# The unavailability the 2026-08-26 kafka session reported for 27,219 tests.
KAFKA_SESSION_REASON = "current receipt testcase rows were unavailable"

RED_SUITE = "kafka-red-suite.xml"
GREEN_SUITE = "kafka-green-suite.xml"
# What the fixtures' own `<testsuite>` roots declare.
RED_SUITE_TESTS = 19
RED_SUITE_FAILURES = 1
GREEN_SUITE_TESTS = 1


def _fixture(name) -> bytes:
    """A committed report's bytes, or a generated report's bytes verbatim.

    The kafka-scale reactor below is 1,176 reports; committing them would be
    committing 3 MB of XML to prove an arithmetic bound. They are generated to
    the measured SHAPE instead (files, executions, reds, skips) and travel the
    same path as the committed ones — written to disk, hashed, and parsed by
    the real readers.
    """

    if isinstance(name, (bytes, bytearray)):
        return bytes(name)
    return (FIXTURES / name).read_bytes()


# --- a Gradle reactor on a real tree ---------------------------------------


ROOT = "/workspace/proj"


class GradleReactor:
    """The measured report layout, on disk for real and mounted at `/workspace`.

    Every report is a real file holding real fixture bytes, so both readers do
    real work on real XML; `mount` is what lets them do it while the receipt
    keeps the only paths its own manifest will accept.
    """

    def __init__(self, store: Path, layout):
        self.root = ROOT
        self.store = store
        self.mount: dict[str, Path] = {}
        self.by_real: dict[str, str] = {}
        self.reports = []
        for module, task, fixture, stem in layout:
            relative = f"{module}/" if module else ""
            relative += f"build/test-results/{task}/TEST-{stem}.xml"
            real = store / relative
            real.parent.mkdir(parents=True, exist_ok=True)
            real.write_bytes(_fixture(fixture))
            container = f"{ROOT}/{relative}"
            self.mount[container] = real
            self.by_real[str(real)] = container
            self.reports.append((container, fixture, module or "root", task))
        modules = [module for _, _, module, _ in self.reports]
        self.modules = list(dict.fromkeys(modules))
        includes = "\n".join(f"include ':{module}'" for module in self.modules if module != "root")
        self.settings = f"rootProject.name = 'proj'\n{includes}\n"

    @property
    def paths(self):
        return sorted(self.mount)

    def real(self, path: str) -> Path:
        return self.mount[path]

    def container(self, real: str) -> str:
        return self.by_real[str(real)]

    def digest(self, path: str) -> str:
        return sha256(self.mount[path].read_bytes()).hexdigest()

    def task_stream(self, outcome: str = "") -> str:
        """Gradle's own per-task lines, naming the TASKS this layout ran.

        One line per (project, task dir) pair the reports were written under,
        because that is what Gradle prints and what the cached-report roots are
        derived from. `outcome` is the trailing word — `FROM-CACHE` when the
        build served the reports rather than rewriting them.
        """
        suffix = f" {outcome}" if outcome else ""
        pairs = dict.fromkeys(
            (module, task) for _, _, module, task in self.reports if module != "root"
        )
        lines = [f"> Task :{module}:{task}{suffix}" for module, task in pairs]
        lines = lines or [f"> Task :test{suffix}"]
        return "\n".join([*lines, "BUILD SUCCESSFUL in 3s"])

    def snapshot(self) -> str:
        return "".join(f"{self.digest(path)}  {path}\n" for path in self.paths)

    def discovery(self) -> str:
        body = "".join(f"{path}\n" for path in self.paths)
        return f"{body}{COUNT_MARKER}{len(self.paths)} 0\n"


class ReactorOrchestrator(ReceiptOrchestrator):
    """`ReceiptOrchestrator`, answering every read the harvest and the seal make.

    Each branch below is a transport the production code chose, answered the
    way a container would answer it. The two Python readers are not answered at
    all — they are RUN, on this interpreter, over the fixture bytes on disk, so
    the rows this receipt seals are a real parse of real JUnit XML.
    """

    def __init__(
        self,
        reactor: GradleReactor,
        *,
        tmp_path: Path,
        reports_on_disk=True,
        served_from_cache=False,
    ):
        # A cache hit rewrites nothing: the reports are on disk on BOTH sides
        # of the bracket, byte-identical, and only the task stream's FROM-CACHE
        # makes them this dispatch's. That is kafka's `--build-cache` run.
        before = reactor.snapshot() if served_from_cache else ""
        super().__init__(
            snapshots=[before, reactor.snapshot() if reports_on_disk else ""],
            monitored_result={
                "output": reactor.task_stream("FROM-CACHE" if served_from_cache else ""),
                "exit_code": 0,
            },
            build_system="gradle",
        )
        self.reactor = reactor
        self.tmp_path = tmp_path
        self.reports_on_disk = reports_on_disk
        self.files = {}
        self.harvest_commands = []
        # Every command the run issued, in order — the branches below answer
        # some of them without reaching the base double's own log.
        self.issued = []

    # -- the two readers, run for real --------------------------------------

    def _run(self, script: str, payload):
        """Run one in-container reader here, over the mounted reports."""
        source = self.tmp_path / "reader-input.json"
        source.write_text(json.dumps(payload))
        completed = subprocess.run(
            [sys.executable, "-c", script, str(source)],
            capture_output=True,
            text=True,
            check=True,
        )
        return {"exit_code": 0, "output": completed.stdout, "success": True}

    def _input_payload(self, command):
        import shlex

        return json.loads(self.files[shlex.split(command)[-1]])

    def _run_row_parser(self, command):
        payload = [
            {**entry, "path": str(self.reactor.real(entry["path"]))}
            for entry in self._input_payload(command)
        ]
        result = self._run(_row_parser_program(), payload)
        parsed = json.loads(result["output"])
        for row in parsed.get("rows") or ():
            row["report_path"] = self.reactor.container(row["report_path"])
        return {**result, "output": json.dumps(parsed)}

    def _run_head_reader(self, command):
        payload = [str(self.reactor.real(path)) for path in self._input_payload(command)]
        script = _GRADLE_SUITE_HEAD_READER.replace("HEAD_BYTES", str(GRADLE_SUITE_HEAD_BYTES))
        result = self._run(script, payload)
        parsed = json.loads(result["output"])
        for suite in parsed.get("suites") or ():
            suite["path"] = self.reactor.container(suite["path"])
        return {**result, "output": json.dumps(parsed)}

    # -- the container's own command shapes ---------------------------------

    def execute_command(self, command, workdir=None, timeout=None, **kwargs):
        self.issued.append(command)
        if RECEIPT_DIR in command:
            return super().execute_command(command, workdir=workdir, timeout=timeout, **kwargs)
        if "import xml.etree.ElementTree as ET" in command:
            self.harvest_commands.append(command)
            return self._run_row_parser(command)
        if "ATTRIBUTE = re.compile" in command:
            self.harvest_commands.append(command)
            return self._run_head_reader(command)
        if REPORT_MARKER in command:
            self.harvest_commands.append(command)
            return {"exit_code": 0, "output": self._tag_stream(command), "success": True}
        if COUNT_MARKER in command:
            self.harvest_commands.append(command)
            body = self.reactor.discovery() if self.reports_on_disk else f"{COUNT_MARKER}0 0\n"
            return {"exit_code": 0, "output": body, "success": True}
        if command.startswith("git -C ") and command.endswith("rev-parse HEAD"):
            return {"exit_code": 0, "output": f"{TARGET_SHA}\n", "success": True}
        if command.startswith("test -f ") and " && cat " in command:
            return self._read_settings(command)
        if "/workspace/.setup_agent/." in command:
            return self._bounded_write(command)
        return super().execute_command(command, workdir=workdir, timeout=timeout, **kwargs)

    def _read_settings(self, command):
        """Exactly one static settings file, as `read_gradle_project_map` demands."""
        if "settings.gradle.kts" in command or "settings.gradle" not in command:
            return {"exit_code": 1, "output": "", "success": False}
        return {"exit_code": 0, "output": self.reactor.settings, "success": True}

    def _tag_stream(self, command):
        """`grep -oE` over each claimed report, headed by that report's digest.

        Line-oriented, because `grep` is: a tag the bound split across lines is
        a tag the real transport would not have produced either.
        """
        chunks = []
        for path in re.findall(r"sha256sum (\S+) 2>/dev/null", command):
            path = path.strip("'")
            text = self.reactor.real(path).read_text()
            tags = [
                match.group(0)
                for line in text.splitlines()
                for match in re.finditer(TESTCASE_TAG_PATTERN, line)
            ]
            body = "\n".join(tags)
            chunks.append(f"{REPORT_MARKER}{self.reactor.digest(path)}  {path}\n{body}\n")
        return "".join(chunks)

    def _bounded_write(self, command):
        """The shared atomic write, enough of it to hold a reader's input list."""
        import base64
        import hashlib
        import shlex

        tokens = shlex.split(command)
        if tokens[:3] == ["mkdir", "-p", "--"]:
            return {"exit_code": 0, "output": "", "success": True}
        if tokens[:2] == ["rm", "-f"]:
            for target in tokens[3:] if tokens[2:3] == ["--"] else tokens[2:]:
                self.files.pop(target, None)
            return {"exit_code": 0, "output": "", "success": True}
        if tokens[:2] == [":", ">"]:
            self.files[tokens[2]] = ""
            return {"exit_code": 0, "output": "", "success": True}
        if tokens[:2] == ["printf", "%s"] and tokens[3] == ">>":
            self.files[tokens[4]] = self.files.get(tokens[4], "") + tokens[2]
            return {"exit_code": 0, "output": "", "success": True}
        if tokens[:2] == ["base64", "--decode"] and tokens[-2:-1] == [">"]:
            decoded = base64.b64decode(self.files.get(tokens[2], "")).decode("utf-8")
            self.files[tokens[-1]] = decoded
            return {"exit_code": 0, "output": "", "success": True}
        if tokens[:2] == ["python3", "-c"] and "hashlib.sha256" in tokens[2]:
            payload = self.files.get(tokens[3], "").encode("utf-8")
            valid = len(payload) == int(tokens[4]) and (
                hashlib.sha256(payload).hexdigest() == tokens[5]
            )
            return {"exit_code": 0 if valid else 1, "output": "", "success": valid}
        if tokens[:2] == ["python3", "-c"] and "json.load" in tokens[2]:
            try:
                json.loads(self.files.get(tokens[3], ""))
            except (TypeError, json.JSONDecodeError):
                return {"exit_code": 1, "output": "", "success": False}
            return {"exit_code": 0, "output": "", "success": True}
        if tokens[:2] == ["mv", "-f"]:
            self.files[tokens[-1]] = self.files.pop(tokens[-2], "")
            return {"exit_code": 0, "output": "", "success": True}
        return {"exit_code": 0, "output": "", "success": True}


def _run_reactor(
    tmp_path,
    layout,
    *,
    reports_on_disk=True,
    served_from_cache=False,
    action="test",
):
    """Drive one Gradle dispatch and return `(receipt, orchestrator)`.

    `action` is whatever the build calls its test work. Nothing downstream of
    the argv may read it as a category: since r2-T4 the harvest is decided by
    the report delta, so `smokeTest` and `test` take the same path here.
    """

    store = tmp_path / "container"
    store.mkdir(exist_ok=True)
    reactor = GradleReactor(store, layout)
    orchestrator = ReactorOrchestrator(
        reactor,
        tmp_path=tmp_path,
        reports_on_disk=reports_on_disk,
        served_from_cache=served_from_cache,
    )
    with argv_contract_authority(
        executor="gradle",
        action=action,
        expected_argv=f"--build-cache {action}",
        cwd=ROOT,
    ):
        GradleTool(orchestrator).execute(tasks=action, working_directory=ROOT)
    (receipt,) = receipts_written(orchestrator.receipt_commands)
    return receipt, orchestrator


CLIENTS_LAYOUT = [
    ("clients", "test", RED_SUITE, "ConfigurationUtilsTest"),
    ("clients", "test", GREEN_SUITE, "ProtocolTest"),
]
# kafka's three red-bearing modules, as the study named them.
RED_BEARING_LAYOUT = [
    (module, "test", RED_SUITE, "ConfigurationUtilsTest")
    for module in ("clients", "metadata", "streams")
]

# --- the measured kafka reactor, at its measured scale -----------------------
#
# `docs/superpowers/reports/gradle-evidence-20260830.md`: the 2026-08-26 kafka
# session wrote 1,176 JUnit XML files across 19 modules holding 27,219 executed
# tests, 8 of them red and 32 skipped, and reported none of it.
KAFKA_MODULES = (
    "clients",
    "connect-api",
    "connect-json",
    "connect-runtime",
    "core",
    "metadata",
    "raft",
    "server-common",
    "storage",
    "streams",
    "streams-scala",
    "tools",
    "trogdor",
    "group-coordinator",
    "transaction-coordinator",
    "shell",
    "examples",
    "jmh-benchmarks",
    "generator",
)
KAFKA_REPORTS = 1176
KAFKA_TESTS = 27_219
KAFKA_REDS = 8
KAFKA_SKIPPED = 32
# The red-bearing modules the study cross-checked against the session's own
# module-failure records.
KAFKA_RED_MODULES = ("clients", "metadata", "streams")


def _kafka_report(module: str, index: int, tests: int, reds: int, skips: int) -> bytes:
    """One report, declaring in its `<testsuite>` root exactly what it holds."""

    suite = f"org.apache.kafka.{module.replace('-', '.')}.Suite{index:04d}Test"
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name="{suite}" tests="{tests}" skipped="{skips}" '
        f'failures="{reds}" errors="0" time="1.5">',
    ]
    for ordinal in range(tests):
        case = f'<testcase name="case{ordinal:03d}()" classname="{suite}"'
        if ordinal < reds:
            lines.append(
                f'{case}><failure message="expected: &lt;true&gt; but was: &lt;false&gt;" '
                'type="org.opentest4j.AssertionFailedError"/></testcase>'
            )
        elif ordinal < reds + skips:
            lines.append(f"{case}><skipped/></testcase>")
        else:
            lines.append(f"{case}/>")
    lines.append("</testsuite>")
    return "\n".join(lines).encode("utf-8")


def _kafka_scale_layout():
    """1,176 reports over 19 modules, summing to the measured 27,219/8/32."""

    base, extra = divmod(KAFKA_TESTS, KAFKA_REPORTS)
    red_indices = [
        index
        for index in range(KAFKA_REPORTS)
        if KAFKA_MODULES[index % len(KAFKA_MODULES)] in KAFKA_RED_MODULES
    ][:KAFKA_REDS]
    skip_indices = set(range(100, 100 + KAFKA_SKIPPED * 7, 7))
    layout = []
    for index in range(KAFKA_REPORTS):
        module = KAFKA_MODULES[index % len(KAFKA_MODULES)]
        tests = base + (1 if index < extra else 0)
        reds = 1 if index in set(red_indices) else 0
        skips = 1 if index in skip_indices else 0
        layout.append(
            (
                module,
                "test",
                _kafka_report(module, index, tests, reds, skips),
                f"Suite{index:04d}Test",
            )
        )
    return layout


def _metrics(receipt):
    """The production metrics assembly, over this run's receipt ledger."""

    return assemble_report_metrics(
        snapshot={
            "verdict": "partial",
            "phase_records": [{"phase": "test", "termination": "complete"}],
            "build_evidence": {"observed": True, "judgment": "success"},
        },
        build_evidence={},
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-08-30T12:00:00Z",
        run_pin={
            "run_id": receipt["run_id"],
            "target_repo_sha": receipt["target_sha"],
        },
        persistence={
            "receipts_expected": 1,
            "receipts_persisted": 1,
            "terminal_receipts_unpersisted": 0,
        },
        receipt_records=[receipt],
    )


# --- the chain, end to end -------------------------------------------------


def test_a_gradle_test_run_reaches_the_metrics_surface_the_kafka_session_could_not(
    tmp_path,
):
    """The whole point: `receipt_executions` states a number, not that reason.

    One module, the two committed kafka suites: 19 + 1 declared tests, one
    declared failure. Every number below is carried by production code from
    the XML on disk to the metrics contract, and the run that motivated this
    reported none of it.
    """
    receipt, _ = _run_reactor(tmp_path, CLIENTS_LAYOUT)

    # The receipt itself passes the live ledger reader's own two gates: the
    # strict validator called against the record's filename identity, and the
    # run scoping that decides whether the ledger may be read at all.
    assert validate_receipt_v2(receipt, expected_id=receipt["receipt_id"]) == receipt
    assert receipt_record_scope(receipt, receipt["run_id"]) == "current"

    metrics = _metrics(receipt)
    executions = metrics["tests"]["claimed"]["receipt_executions"]
    assert executions["availability"] == "available"
    assert executions.get("reason") is None
    assert executions["executed"] == RED_SUITE_TESTS + GREEN_SUITE_TESTS
    assert executions["failed"] == RED_SUITE_FAILURES
    assert executions["passed"] == RED_SUITE_TESTS + GREEN_SUITE_TESTS - RED_SUITE_FAILURES
    assert executions["errors"] == 0
    # The identity grains come up with it, which is what `unavailable` denied.
    assert metrics["tests"]["claimed"]["latest_cases"]["executed"] == 20
    assert metrics["tests"]["claimed"]["latest_subjects"]["failed"] == 1
    assert metrics["tests"]["unattributed_observations"]["executed"] == 0


def test_the_run_counts_downstream_are_the_counts_the_reports_declared(tmp_path):
    """Two independent measurements of the same run agree.

    Tier 1 sums what each report's `<testsuite>` root declares; the sealed rows
    are counted one at a time by a different reader entirely. They are separate
    fields on purpose, and a disagreement between them would mean one of the
    two transports is lying about the same disk.
    """
    receipt, _ = _run_reactor(tmp_path, CLIENTS_LAYOUT)

    (suite,) = receipt["gradle_suite_summaries"]["suites"]
    assert suite == {
        "module": ":clients",
        "task": "test",
        "xml_files": 2,
        "tests": RED_SUITE_TESTS + GREEN_SUITE_TESTS,
        "failures": RED_SUITE_FAILURES,
        "errors": 0,
        "skipped": 0,
    }
    declared = suite["tests"]
    sealed = _metrics(receipt)["tests"]["claimed"]["receipt_executions"]["executed"]
    assert declared == sealed == 20
    # And the module witness the task stream alone could never state.
    assert receipt["module_outcomes"] == [
        {"module": "clients", "status": "attempted", "tests_reported": 20}
    ]


def test_the_red_identity_survives_every_hop_to_the_consumers(tmp_path):
    """A count without a name is not actionable; the name has to arrive too.

    `testFileUnreadable` is one of the eight measured kafka reds. It is checked
    here in each of the three places a consumer can look: the bounded
    diagnostic list, the sealed rows, and the shared aggregation law that
    `report_metrics` counts with.
    """
    receipt, _ = _run_reactor(tmp_path, CLIENTS_LAYOUT)

    diagnostic = receipt["testcase_outcomes"]["nodes"]
    red = [node for node in diagnostic if node["status"] == "failed"]
    assert [node["node_id"] for node in red] == [RED_NODE]
    # Red sorts first, so a bound that fires cannot be what drops it.
    assert diagnostic[0]["node_id"] == RED_NODE
    assert "AssertionFailedError" in red[0]["reason"]

    envelope = receipt["testcase_execution_rows"]
    assert envelope["status"] == "complete"
    rows = [
        validate_testcase_execution_row(
            row,
            receipt_id=receipt["receipt_id"],
            run_id=receipt["run_id"],
            target_sha=receipt["target_sha"],
            domain_id=receipt["domain_id"],
            report_claims={
                (entry["path"], entry["sha256"])
                for bucket in ("new", "changed", "cached")
                for entry in receipt["report_delta"].get(bucket) or ()
            },
        )
        for row in envelope["rows"]
    ]
    failed = [row for row in rows if row["outcome"] == "failed"]
    assert [row["test_name"] for row in failed] == ["testFileUnreadable"]
    # Module-qualified, by Gradle's own project path, proved twice over.
    assert {row["module_coordinate"] for row in rows} == {":clients"}
    assert {row["framework"] for row in rows} == {"junit-xml"}
    assert failed[0]["execution_id"] == execution_id_of(failed[0])

    aggregation = aggregate_testcase_execution_rows(rows)
    assert aggregation["claimed"]["latest_cases"]["failed"] == 1
    assert aggregation["claimed"]["receipt_executions"]["executed"] == 20


def test_a_bounded_row_sample_states_what_it_dropped_and_keeps_every_red(tmp_path):
    """kafka's three red-bearing modules, past the diagnostic bound.

    57 executions against a 50-node diagnostic cap. The cap fires, the receipt
    says so and by how much, and all three failure identities are still there —
    which is the disclosure-ordering principle doing exactly its job.
    """
    receipt, _ = _run_reactor(tmp_path, RED_BEARING_LAYOUT)

    total = RED_SUITE_TESTS * 3
    assert total > TESTCASE_OUTCOME_CAP

    disclosure = receipt["gradle_row_disclosure"]
    assert disclosure["rows_source"] == "gradle_xml"
    assert disclosure["red_rows_complete"] is True
    truncation = disclosure["rows_truncated"]
    assert truncation["dropped_green"] == total - TESTCASE_OUTCOME_CAP
    assert "dropped_red" not in truncation
    # The sample says it is a sample, in the field a reader of the list sees.
    assert receipt["testcase_outcomes"]["truncated"] is True

    # Every red identity is present, one per module, and the module-qualified
    # counts downstream are the COMPLETE ones — the cap bounded the identity
    # sample, never the count.
    reds = [node for node in receipt["testcase_outcomes"]["nodes"] if node["status"] == "failed"]
    assert [node["node_id"] for node in reds] == [RED_NODE]
    assert {suite["module"] for suite in receipt["gradle_suite_summaries"]["suites"]} == {
        ":clients",
        ":metadata",
        ":streams",
    }
    executions = _metrics(receipt)["tests"]["claimed"]["receipt_executions"]
    assert executions["executed"] == total
    assert executions["failed"] == RED_SUITE_FAILURES * 3
    rows = receipt["testcase_execution_rows"]["rows"]
    assert {row["module_coordinate"] for row in rows} == {":clients", ":metadata", ":streams"}


def test_geodes_second_task_dir_is_counted_as_its_own_suite_and_the_same_module(tmp_path):
    """`test-results/distributedTest` beside `test-results/test`, all the way down.

    geode-core is the module the listing fixture caught doing this, and it is
    why the summary unit is the (project, task-dir) PAIR: a rollup keyed on the
    module alone would have merged two different task runs into one row and a
    glob hard-coded to `test-results/test` would have lost the distributed
    suites entirely. The rows, meanwhile, belong to one project — the task dir
    is where a report was written, never who owns the test.
    """
    receipt, _ = _run_reactor(
        tmp_path,
        [
            ("geode-core", "test", GREEN_SUITE, "ProtocolTest"),
            ("geode-core", "distributedTest", RED_SUITE, "ConfigurationUtilsTest"),
        ],
    )

    assert receipt["gradle_suite_summaries"]["suites"] == [
        {
            "module": ":geode-core",
            "task": "distributedTest",
            "xml_files": 1,
            "tests": RED_SUITE_TESTS,
            "failures": RED_SUITE_FAILURES,
            "errors": 0,
            "skipped": 0,
        },
        {
            "module": ":geode-core",
            "task": "test",
            "xml_files": 1,
            "tests": GREEN_SUITE_TESTS,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
        },
    ]
    assert receipt["module_outcomes"] == [
        {"module": "geode-core", "status": "attempted", "tests_reported": 20}
    ]

    rows = receipt["testcase_execution_rows"]["rows"]
    assert {row["module_coordinate"] for row in rows} == {":geode-core"}
    executions = _metrics(receipt)["tests"]["claimed"]["receipt_executions"]
    assert executions["executed"] == RED_SUITE_TESTS + GREEN_SUITE_TESTS
    assert executions["failed"] == RED_SUITE_FAILURES


def test_a_task_name_no_allowlist_ever_had_flows_end_to_end(tmp_path):
    """`smokeTest` and `verify-integration`, from the dispatch to the metrics.

    Three separate name gates stood between this run and its own evidence: the
    action allowlist that decided whether to harvest at all, the task regex
    that could not read a hyphen, and the cached-roots list of three names.
    None of them exists any more, so a build whose suite is called whatever the
    build script called it reports exactly what a `test` run does.
    """
    receipt, _ = _run_reactor(
        tmp_path,
        [
            ("payments", "smokeTest", GREEN_SUITE, "ProtocolTest"),
            ("payments", "verify-integration", RED_SUITE, "ConfigurationUtilsTest"),
        ],
        action="smokeTest",
    )

    assert [suite["task"] for suite in receipt["gradle_suite_summaries"]["suites"]] == [
        "smokeTest",
        "verify-integration",
    ]
    assert receipt["module_outcomes"] == [
        {"module": "payments", "status": "attempted", "tests_reported": 20}
    ]
    executions = _metrics(receipt)["tests"]["claimed"]["receipt_executions"]
    assert executions["executed"] == RED_SUITE_TESTS + GREEN_SUITE_TESTS
    assert executions["failed"] == RED_SUITE_FAILURES


def test_a_cached_custom_task_is_claimed_and_counted_like_any_other(tmp_path):
    """geode's `distributedTest`, served FROM-CACHE — the r1 blind spot.

    Nothing was rewritten: the reports are byte-identical across the bracket
    and only Gradle's own FROM-CACHE makes them this dispatch's. The cached
    roots knew `("test", "integrationTest", "check")`, so the delta claimed
    nothing and the receipt declared an absence over reports sitting on disk.
    """
    receipt, _ = _run_reactor(
        tmp_path,
        [
            ("geode-core", "distributedTest", RED_SUITE, "ConfigurationUtilsTest"),
            ("payments", "verify-integration", GREEN_SUITE, "ProtocolTest"),
        ],
        served_from_cache=True,
        action="distributedTest",
    )

    assert validate_receipt_v2(receipt) == receipt
    assert receipt["report_delta"]["new"] == [] and receipt["report_delta"]["changed"] == []
    assert [entry["path"].rsplit("/", 2)[-2] for entry in receipt["report_delta"]["cached"]] == [
        "distributedTest",
        "verify-integration",
    ]
    assert "evidence_omissions" not in receipt
    executions = _metrics(receipt)["tests"]["claimed"]["receipt_executions"]
    assert executions["executed"] == RED_SUITE_TESTS + GREEN_SUITE_TESTS
    assert executions["failed"] == RED_SUITE_FAILURES


def test_a_test_run_that_left_no_reports_is_missing_downstream_and_never_zero(tmp_path):
    """ofbiz-plugins: the task ran and wrote nothing.

    The receipt declares the omission on the engine's closed vocabulary, the
    metrics surface COUNTS that declaration, and `receipt_executions` stays
    unknown. A zero here would be the worst possible answer — it reads as a
    clean project with no tests.

    No plan is sealed in this run, so nothing states whether a report was ever
    due — and the receipt says so beside the measurement instead of letting a
    bare absence read as evidence that tests were expected.
    """
    receipt, _ = _run_reactor(tmp_path, [], reports_on_disk=False)

    assert validate_receipt_v2(receipt) == receipt
    assert "gradle_suite_summaries" not in receipt
    assert "gradle_row_disclosure" not in receipt
    assert receipt["evidence_omissions"] == [
        {
            "field": "gradle_row_disclosure",
            "status": "unavailable",
            "reasons": [
                "gradle_no_test_reports_on_disk",
                "gradle_test_absence_disposition_unknown",
            ],
        },
        {
            "field": "gradle_suite_summaries",
            "status": "unavailable",
            "reasons": [
                "gradle_no_test_reports_on_disk",
                "gradle_test_absence_disposition_unknown",
            ],
        },
    ]

    metrics = _metrics(receipt)
    assert metrics["evidence"]["evidence_omissions"] == 2
    executions = metrics["tests"]["claimed"]["receipt_executions"]
    assert executions["executed"] is None
    assert executions["reason"] == KAFKA_SESSION_REASON


def test_no_command_this_run_issued_can_ship_a_report_whole(tmp_path):
    """The trust boundary the whole transport design exists to hold.

    Every command that so much as NAMES a report is checked here, not just the
    ones a reader remembered to log. Three shapes are allowed to name one — the
    hash bracket, the discovery glob, and the bounded per-report tag read — and
    each of them is bounded by construction. The two parsers name no report at
    all, because their path lists travel as a file: that is what keeps a
    reactor of 1,176 reports away from ARG_MAX, and it is why nothing here can
    become a 137.8 MB argument.
    """
    receipt, orchestrator = _run_reactor(tmp_path, CLIENTS_LAYOUT)

    naming = [command for command in orchestrator.issued if "/build/test-results/" in command]
    assert naming, "the run must have looked at its reports at all"
    for command in naming:
        assert "cat " not in command
        bracket = "-exec sha256sum" in command
        discovery = "find " in command and COUNT_MARKER in command
        bounded_tags = REPORT_MARKER in command and "head -n" in command
        assert bracket or discovery or bounded_tags, command
    assert not any("python3 -c" in command for command in naming)
    # And the delta the receipt claims is the bracket's, not a scan's.
    assert {entry["path"] for entry in receipt["report_delta"]["new"]} == set(
        orchestrator.reactor.paths
    )


# --- conformance: the sections ride schema v2 without disturbing it ---------


def _canonical(value) -> str:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _has_null(value) -> bool:
    """Whether any position anywhere in this document is an explicit null."""

    if value is None:
        return True
    if isinstance(value, dict):
        return any(_has_null(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_null(item) for item in value)
    return False


def test_a_gradle_receipt_round_trips_its_canonical_bytes_unchanged(tmp_path):
    """Serialize, read back, validate, serialize: byte for byte the same.

    The receipt is content-addressed evidence. A section that survived
    validation but re-serialized differently would break every digest taken
    over it, so the Gradle sections have to be as inert on the wire as every
    other v2 fact.
    """
    receipt, _ = _run_reactor(tmp_path, RED_BEARING_LAYOUT)

    assert receipt["schema_version"] == RECEIPT_SCHEMA_VERSION == 3
    assert {"gradle_suite_summaries", "gradle_row_disclosure"}.issubset(receipt)

    first = _canonical(receipt)
    revalidated = validate_receipt_v2(json.loads(first))
    assert _canonical(revalidated) == first
    assert _canonical(validate_receipt_v2(json.loads(_canonical(revalidated)))) == first


def test_one_sealed_row_stays_under_the_ceiling_the_structural_guard_assumes(tmp_path):
    """The guard's constants are ceilings over real rows, not round numbers.

    `invocation_receipts` asserts a constant relation at import: the bounded
    rows section's worst case — cap x one sealed row's ceiling — stays a
    quarter of the receipt's canonical budget. That assertion is only worth
    what its per-row ceiling is worth, so it is measured here against real
    kafka identities, and the whole relation is recomputed rather than
    trusted.

    The count side needs no such guard, which is the argument for keeping it a
    separate section: a whole reactor's totals fit in a few hundred entries at
    kafka scale and at ofbiz scale alike.
    """
    receipt, _ = _run_reactor(tmp_path, CLIENTS_LAYOUT)

    rows = receipt["testcase_execution_rows"]["rows"]
    widest = max(len(_canonical(row).encode("utf-8")) for row in rows)

    # Real kafka identities, so the cost is the measured one, not a toy's.
    assert 500 < widest < SEALED_ROW_MAX_CANONICAL_BYTES
    assert ROW_SECTION_MAX_CANONICAL_BYTES == (
        DELTA_TESTCASE_ROW_CAP * SEALED_ROW_MAX_CANONICAL_BYTES
    )
    assert ROW_SECTION_MAX_CANONICAL_BYTES * ROW_SECTION_BUDGET_SHARE < RECEIPT_MAX_CANONICAL_BYTES
    # The unbounded read this replaced: one measured run's 27,219 executions,
    # at the cost measured above, against the budget that refuses a receipt
    # WHOLE — argv, exit code and report delta included.
    assert KAFKA_TESTS * widest > RECEIPT_MAX_CANONICAL_BYTES
    assert len(_canonical(receipt["gradle_suite_summaries"]).encode("utf-8")) < 1024


def test_a_kafka_scale_delta_persists_its_receipt_instead_of_bursting_it(tmp_path):
    """The regression this feature exists for, at the scale that broke it.

    1,176 reports and 27,219 executed tests, the measured 2026-08-26 kafka
    shape, driven through the same real transports as every test above. Before
    the universal bounds this delta sealed 27,219 identity rows into one
    receipt and the write-time budget refused the whole thing — no exit code,
    no argv, no report delta, no evidence of any kind that the run happened.

    What the receipt is required to hold now:

    - itself: the dispatch's own facts, past the strict validator;
    - the COMPLETE totals — every one of the 27,219 executions, summed from
      the reports' own `<testsuite>` roots, not from the sample;
    - a bounded identity sample, capped exactly where the caps say;
    - all eight reds, because red-first retention is what a bound is for;
    - exact drop accounting: kept + dropped == observed, to the row;
    - canonical bytes comfortably under the budget that used to refuse it.
    """
    receipt, orchestrator = _run_reactor(tmp_path, _kafka_scale_layout())

    # 1. It exists, and it is a receipt — the strict live reader's own gates.
    assert validate_receipt_v2(receipt, expected_id=receipt["receipt_id"]) == receipt
    assert receipt_record_scope(receipt, receipt["run_id"]) == "current"
    assert receipt["exit_code"] == 0
    assert receipt["argv"].endswith("--build-cache test")
    assert len(receipt["report_delta"]["new"]) == KAFKA_REPORTS

    # 2. The totals are complete. No bound below touches them.
    suites = receipt["gradle_suite_summaries"]["suites"]
    assert len(suites) == len(KAFKA_MODULES)
    assert sum(suite["tests"] for suite in suites) == KAFKA_TESTS
    assert sum(suite["failures"] for suite in suites) == KAFKA_REDS
    assert sum(suite["errors"] for suite in suites) == 0
    assert sum(suite["skipped"] for suite in suites) == KAFKA_SKIPPED
    assert sum(module["tests_reported"] for module in receipt["module_outcomes"]) == KAFKA_TESTS

    # 3. The identities are a sample, capped where the cap says.
    rows = receipt["testcase_execution_rows"]["rows"]
    assert receipt["testcase_execution_rows"]["status"] == "complete"
    assert len(rows) == DELTA_TESTCASE_ROW_CAP

    # 4. Every red survived it. That is the whole ordering principle.
    reds = [row for row in rows if row["outcome"] in {"failed", "error"}]
    assert len(reds) == KAFKA_REDS
    assert {row["module_coordinate"] for row in reds} == {
        f":{module}" for module in KAFKA_RED_MODULES
    }

    # 5. The drops are stated exactly, and they reconcile with the observation.
    disclosure = receipt["testcase_row_disclosure"]
    assert disclosure["rows_source"] == TESTCASE_ROWS_SOURCE
    assert disclosure["red_rows_complete"] is True
    truncation = disclosure["rows_truncated"]
    assert "dropped_red" not in truncation
    assert truncation["dropped_green"] == KAFKA_TESTS - DELTA_TESTCASE_ROW_CAP
    assert len(rows) + truncation["dropped_green"] == KAFKA_TESTS
    carried = {row["report_path"] for row in rows}
    assert truncation["dropped_files"] == KAFKA_REPORTS - len(carried)

    # 6. And it fits, with the budget to spare that the guard promises.
    canonical = len(_canonical(receipt).encode("utf-8"))
    assert canonical < RECEIPT_MAX_CANONICAL_BYTES
    assert canonical < ROW_SECTION_MAX_CANONICAL_BYTES * ROW_SECTION_BUDGET_SHARE

    # 7. Downstream reads it: the run that reported "unavailable" for 27,219
    # tests now states numbers, and names its failures.
    executions = _metrics(receipt)["tests"]["claimed"]["receipt_executions"]
    assert executions["availability"] == "available"
    assert executions.get("reason") != KAFKA_SESSION_REASON
    assert executions["failed"] == KAFKA_REDS
    assert executions["executed"] == len(rows)
    assert orchestrator.reactor.paths


@pytest.mark.parametrize(
    "tool, summaries, disclosure",
    [
        ("maven", None, None),
        ("gradle", None, None),
        (
            "gradle",
            {
                "suites": [
                    {
                        "module": ":root",
                        "task": "test",
                        "xml_files": 1,
                        "tests": 1,
                        "failures": 0,
                        "errors": 0,
                        "skipped": 0,
                    }
                ]
            },
            None,
        ),
    ],
)
def test_absent_gradle_evidence_stays_absent_never_null(tool, summaries, disclosure):
    """A section nobody harvested is a missing key, not a null and not a zero.

    This is the rule the whole v2 receipt is built on, restated for the two new
    fields: a maven receipt never grows a Gradle section, and a Gradle receipt
    that harvested one measurement and not the other carries exactly the one it
    made.
    """
    receipt = build_receipt(
        receipt_id=f"{tool}-0001",
        run_id="run-gradle-e2e",
        tool=tool,
        requested_action="test",
        effective_action="test",
        argv="test",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={},
        gradle_suite_summaries=summaries,
        gradle_row_disclosure=disclosure,
    )

    assert validate_receipt_v2(receipt) == receipt
    for field, value in (
        ("gradle_suite_summaries", summaries),
        ("gradle_row_disclosure", disclosure),
    ):
        assert (field in receipt) is (value is not None)
    # Not one null anywhere: "unknown" is spelled by a key that is not there.
    assert not _has_null(receipt)


# --- the standing proof that downstream needed no Gradle exception ----------


def _maven_twin(receipt):
    """The same run, the same rows, re-pointed at Maven's report layout.

    Same identities, same outcomes, same delta — only the runner and the report
    boundary differ. Anything the projection does differently to these two is a
    runner assumption, and a runner assumption is exactly what would have
    blocked Gradle from ingesting.
    """
    twin = json.loads(_canonical(receipt))
    twin["tool"] = "maven"
    for section in ("gradle_suite_summaries", "gradle_row_disclosure"):
        twin.pop(section, None)

    def remap(path: str) -> str:
        head, _, tail = path.partition("/build/test-results/")
        return f"{head}/target/surefire-reports/{tail.split('/', 1)[1]}"

    for bucket in ("new", "changed", "cached"):
        for entry in twin["report_delta"].get(bucket) or ():
            entry["path"] = remap(entry["path"])
    twin["module_outcomes"] = [
        {"module": entry["module"], "status": "success"} for entry in twin["module_outcomes"]
    ]
    for row in twin["testcase_execution_rows"]["rows"]:
        row["report_path"] = remap(row["report_path"])
        # Maven's module coordinate is the report's own directory boundary.
        row["module_coordinate"] = row["module_coordinate"].lstrip(":")
        row["execution_id"] = execution_id_of(row)
    return validate_receipt_v2(twin)


def test_the_row_ingestion_is_runner_agnostic_so_no_downstream_change_was_needed(
    tmp_path,
):
    """The same rows, ingested identically whichever runner sealed them.

    This is the assertion behind "nothing downstream was modified". The metrics
    projection was written for Maven's receipts; if it carried a Maven-only
    assumption — a runner check, a report-path shape, a `rows_source` it
    recognized — the Gradle receipt would have projected differently here. It
    does not. Nothing in `report_metrics`, `report_tool` or the row contract
    was touched to make the Gradle chain work, and this test fails if a later
    change makes one of them runner-aware.
    """
    gradle_receipt, _ = _run_reactor(tmp_path, CLIENTS_LAYOUT)
    maven_receipt = _maven_twin(gradle_receipt)

    gradle_claimed = _metrics(gradle_receipt)["tests"]["claimed"]
    maven_claimed = _metrics(maven_receipt)["tests"]["claimed"]

    for grain in ("receipt_executions", "latest_cases", "latest_subjects"):
        assert gradle_claimed[grain] == maven_claimed[grain]
        assert gradle_claimed[grain]["availability"] == "available"
    assert gradle_claimed["receipt_executions"]["executed"] == 20

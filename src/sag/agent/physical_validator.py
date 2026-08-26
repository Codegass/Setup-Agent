"""
Physical Validator for Fact-Based Build and Test Validation

This module provides comprehensive validation of build and test status based on
physical evidence rather than log inference, ensuring accurate status determination.

Key Features:
- Cross-platform compilation timestamp checking (GNU stat, BSD stat, Python fallback)
- Comprehensive build artifact detection (Maven target/, Gradle build/, build/libs/)
- Strict XML test report parsing (Maven Surefire, Gradle test reports)
- TTL-based result caching (60s default) for expensive file system operations
- Unified command execution with standardized error handling and logging
- Logical consistency enforcement (no build without clone, no test without build)

The PhysicalValidator eliminates common issues with log-based inference by directly
examining the file system state and parsing structured test reports.

Example Usage:
    validator = PhysicalValidator(docker_orchestrator=orchestrator)
    build_result = validator.validate_build_artifacts("my-project")
    test_result = validator.parse_test_reports("/workspace/my-project")

    if build_result['valid'] and test_result['test_success']:
        print("Project built and tested successfully!")
"""

import hashlib
import json
import os
import posixpath
import re
import shlex
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

from loguru import logger

from sag.agent.evidence_records import (
    EvidencePublicationBinding,
    json_record_stream_succeeded,
    read_live_published_json_records,
)
from sag.agent.receipt_structure import dispatch_terminated as _dispatch_terminated
from sag.agent.receipt_structure import module_key as _receipt_module_key
from sag.case_census import TestCensus, census_from_catalog_summary, produce_census
from sag.config.settings import DEFAULT_BUILD_COVERAGE_THRESHOLD
from sag.runtime.container_io import (
    ContainerFileReadError,
)
from sag.runtime.container_io import command_did_not_run as _command_did_not_run
from sag.runtime.container_io import (
    read_container_text,
)
from sag.testcases.catalog import (
    RuntimeTestCaseRecord,
    TestCaseCatalog,
    TestCaseDescriptor,
    build_java_test_catalog,
    normalize_testcase_identifier,
)
from sag.testcases.results import (
    CanonicalTestIdentity,
    TestResultObservation,
    aggregate_test_results,
    canonical_test_identity,
)
from sag.utils.container_io import write_container_text_atomic
from sag.verdict_rates import STALE_CONFLICT, execution_sentence, no_execution_sentence


def _census_facts(census: TestCensus) -> Dict[str, Any]:
    """The census as sealable facts — absent keys for what it never measured.

    Absent-when-inapplicable is the established convention on this path
    (``receipt_scoped`` and friends): a run with no module dimension grows no
    null module fields, so recorded snapshots keep verifying byte-identically.
    """
    facts: Dict[str, Any] = {"denominator_basis": census.basis}
    if census.unmeasured_modules:
        facts["denominator_unmeasured_modules"] = census.unmeasured_modules
        facts["denominator_module_total"] = census.total_modules
    if census.conflicts and census.bare_total is not None:
        # Only the rejected total needs sealing; a total the module sum agrees
        # with is the same number the grain already shows.
        facts["denominator_bare_total"] = census.bare_total
    return facts


# top_level.txt names that are install tooling, never the project under test —
# a second deny-list layer under the record selection in
# _installed_top_level_packages (the project's own record could still list a
# tooling name it vendors).
_NON_PROJECT_TOP_LEVEL = {
    "pip",
    "setuptools",
    "wheel",
    "pkg_resources",
    "_distutils_hack",
}

_MAVEN_REACTOR_MAX_DEPTH = 32
_MAVEN_REACTOR_MAX_NODES = 256
_MAVEN_REACTOR_CONFLICT_REASONS = {
    "maven_reactor_probe_unavailable": "reactor filesystem probes are unavailable",
    "maven_reactor_root_unverified": "reactor root could not be verified",
    "maven_module_outside_project": "a declared module escapes the project root",
    "maven_module_unresolved": "a declared module path could not be resolved",
    "maven_module_pom_unreadable": "a declared module POM could not be read",
    "maven_module_pom_outside_project": ("a Maven module POM resolves outside the project root"),
    "maven_module_pom_invalid": "a declared module POM is not valid XML",
    "maven_module_artifact_unresolved": (
        "a verified non-POM Maven leaf has no resolvable artifact expectation"
    ),
    "maven_profile_activation_unresolved": (
        "a module-bearing Maven profile has runtime-dependent activation"
    ),
    "maven_profile_selection_unresolved": (
        "the recorded Maven profile selection could not be parsed exactly"
    ),
    "maven_profile_execution_unfinished": (
        "the newest reactor-bound Maven build or test is still running"
    ),
    "maven_config_changes_graph": ("the nearest .mvn/maven.config contains graph-changing options"),
    "maven_config_outside_project": (
        "the nearest .mvn configuration resolves outside the project root"
    ),
    "maven_config_unreadable": "the nearest .mvn/maven.config could not be read",
    "maven_module_cycle": "the declared module graph contains a cycle",
    "maven_module_depth_exceeded": "the declared module graph exceeds the depth limit",
    "maven_module_cap_exceeded": "the declared module graph exceeds the node limit",
}


class _MavenReactorLimit(Exception):
    """The validator could not prove a bounded Maven reactor snapshot."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


class _MavenReactorCycle(Exception):
    """A recursive Maven module edge points back into the active DFS stack."""

    def __init__(self, members: frozenset[str]):
        super().__init__("maven reactor cycle")
        self.members = members


@dataclass(frozen=True, slots=True)
class _MavenReactorRecord:
    module_dir: str
    pom_content: str
    has_safe_children: bool


@dataclass(frozen=True, slots=True)
class _MavenProfileSelection:
    enabled: frozenset[str] = frozenset()
    disabled: frozenset[str] = frozenset()
    explicit: bool = False
    conservative: bool = False
    conflicts: Tuple[str, ...] = ()
    working_dir: Optional[str] = None


@dataclass(frozen=True, slots=True)
class _MavenReactorSnapshot:
    records: Tuple[_MavenReactorRecord, ...]
    complete: bool
    conflicts: Tuple[str, ...] = ()
    reason: str = ""
    profile_selection: _MavenProfileSelection = _MavenProfileSelection()


@dataclass(frozen=True, slots=True)
class _MavenModuleDeclarations:
    modules: Tuple[str, ...]
    conflicts: Tuple[str, ...] = ()


class _MavenArtifactExpectations(list):
    """List-compatible expectations carrying their immutable source snapshot."""

    def __init__(
        self,
        values: List[Dict[str, str]],
        reactor_snapshot: _MavenReactorSnapshot,
    ):
        super().__init__(values)
        self.reactor_snapshot = reactor_snapshot


def _normalize_dist_name(name: str) -> str:
    """PEP 503 name normalization: case-folded, runs of ``-_.`` collapse to
    ``-`` — 'PyYAML' and 'pyyaml' are the same distribution."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _dist_record_matches(record_dir: str, project_name: str) -> bool:
    """True when a site-packages ``*.dist-info`` / ``*.egg-info`` dir is the
    PROJECT's own record: the distribution-name segment must equal the
    PEP 503-normalized project name, and anything after it must look like a
    version (leading digit) — so ``requests_toolbelt-1.0.dist-info`` never
    matches project ``requests``. A dependency's record never matches."""
    base = record_dir.rstrip("/").rsplit("/", 1)[-1]
    for suffix in (".dist-info", ".egg-info"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    else:
        return False
    wanted = _normalize_dist_name(project_name)
    normalized = _normalize_dist_name(base)
    if normalized == wanted:
        return True
    if not normalized.startswith(f"{wanted}-"):
        return False
    return normalized[len(wanted) + 1 : len(wanted) + 2].isdigit()


# Invocation receipts (Plan 5 Stage B, schema v1/v2). One JSON file per runner
# invocation; the directory name is the cross-lane storage contract and must
# not move. Joined onto the workspace root so production reads
# /workspace/.setup_agent/invocation_receipts while a test can stand a whole
# workspace up in a temp dir. Plan 6 Stage 0's append-only evidence assessments
# live in the documented SIBLING directory (.setup_agent/evidence_assessments),
# which the in-container parser derives from this one.
INVOCATION_RECEIPTS_DIRNAME = ".setup_agent/invocation_receipts"


class _ExpectationScope(NamedTuple):
    """What scoping coverage to the attempted modules produced (Plan 8 §3.5).

    `denominator_modules` is the outcome's OWN statement of which computation set
    the denominator: the attempted list when the narrowing took effect, `None`
    when it refused and the survey's wide expectation list stood. Round two
    computed that authority a second time, in parallel, from "a receipt stated
    some modules" — so a receipt whose modules could not be mapped onto any
    expectation (`build_coverage_scope_unverified` recorded in the same pass)
    still claimed the receipt rung and disarmed the §3.5 minority-scan cap.
    Authority is a property of the denominator a run ACTUALLY used, and this is
    the one computation that knows which that was.
    """

    expectations: List[Dict[str, Any]]
    untried: List[str]
    conflict: Optional[str]
    denominator_modules: Optional[Tuple[str, ...]]


class _AttemptedModules(NamedTuple):
    """What this run's dispatches STATED they attempted, and whether the harness
    will narrow the coverage denominator on that statement (Plan 8 §2 P4).

    Two answers, because collapsing them into one is the bug this plan is about.
    The #17 narrowing shrinks the denominator with this set, so anything that
    removes a module from it makes the build look MORE complete — and three
    rounds each removed something for a good reason and each improved a verdict.

    * ``modules`` — every module ANY readable receipt named, terminal or not.
      A receipt the harness will not trust as a PROVER is still a CLAIMANT: the
      modules a crashed reactor printed are modules this run attempted, so they
      belong in the denominator. Round three deleted them, and a scoped
      `mvn -pl m0` retry beside an OOM-killed 26-module reactor then narrowed the
      denominator to one module and graded GREEN.
    * ``narrowing_licensed`` — False when a statement exists that the harness
      will not narrow on. "No dispatch stated anything" (a single-module build,
      a receipt-free run) is the honest wide default and licenses nothing to
      narrow; "a dispatch stated something we refuse to narrow on" is a
      different fact and must produce the wide denominator AND a cap.
    * ``cap`` — the conflict code naming why, or None. Never a silent None: a
      swallowed exception is the second case above, not the first.
    """

    modules: Tuple[str, ...]
    narrowing_licensed: bool
    cap: Optional[str]


# One clause per reason the denominator could not be narrowed to what ran, used
# by BOTH the warning and the capped reason sentence, so the model can never be
# shown two different explanations of one refusal (spec §2 P3).
_DENOMINATOR_REFUSALS = {
    "build_coverage_scope_unverified": (
        "the build named modules the expectation list does not contain"
    ),
    "build_receipt_not_terminal": (
        "a dispatch that did not end on its own stated the modules it had reached"
    ),
    "build_receipt_scope_unavailable": (
        "the current run, target checkout, and project root pins could not be bound"
    ),
    "build_receipts_unreadable": "this run's invocation receipts could not be read",
    "module_scan_unreadable": "the module scan could not read the tree",
}

# Whether the receipt directory could be READ, which is three answers and not
# two: a probe that threw is not the same fact as a run that wrote no receipts.
_RECEIPTS_PRESENT = "present"
_RECEIPTS_ABSENT = "absent"
_RECEIPTS_UNREADABLE = "unreadable"


# In-container test-report parser (executed via `python3 - <<'PY'`). The four
# header assignments (project_dir, pytest_reports_dir, receipt_claims,
# primary_root) are prepended by _parse_test_reports_compact_in_container.
# Receipt facts have already crossed the strict host-publication boundary; the
# embedded parser never reopens a container evidence ledger on its own.
# Kept as a plain module string so the embedded script needs no f-string brace
# escaping.
#
# Aggregation model (WS7): every testcase is normalized to the canonical
# (module_or_file, class, name, param_id) identity. Per-identity histories are
# ordered only by the explicit sag.attempt_id persisted in JUnit — filenames
# and mtimes are transport details and never decide which observation is latest.
# Raw executions remain diagnostics; primary/unique counts use each history's
# latest status while first/worst/retry/flaky facts remain visible.
#
# Scoping model (Plan 5 Task B2, universal since 2026-08-14): the scan
# discovers candidates, the receipts decide provenance. Only reports CLAIMED by
# a receipt of the primary test coordinate — and whose recorded sha256 still
# matches the file on disk — feed the primary rollup. Everything else is
# auxiliary (visible, never counted) or stale (claimed, superseded,
# quarantined).
#
# The partition is UNCONDITIONAL. It used to be armed on receipt PRESENCE
# (`receipt_scoped = bool(records)`) while exclusion ran against report CLAIMS,
# which inverted the evidence gradient: geode, whose only receipts were two
# `compileJava` receipts claiming nothing, sealed headline 0 over a
# 10,448-execution corpus, while the SAME corpus with the ledger removed took
# the unscoped branch and sealed all 10,448. Deleting attributed evidence
# improved the number. Provenance is a property of each report, so a run with
# no receipts has no attributed reports — not a licence to count every file it
# can see.
_COMPACT_REPORT_PARSER_BODY = '''
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path


def is_pytest_report(s):
    return s.startswith(pytest_reports_dir.rstrip("/") + "/") or "/.setup_agent/pytest-reports/" in s


def is_report_file(path):
    s = str(path)
    if not path.name.endswith(".xml"):
        return False
    return (
        "/target/surefire-reports/" in s
        or "/target/failsafe-reports/" in s
        or "/build/test-results/" in s
        or is_pytest_report(s)
    )


def local_name(element):
    return element.tag.rsplit("}", 1)[-1]


def normalized_file_path(value):
    if not value:
        return ""
    path = re.sub(r"/+", "/", str(value).strip().replace("\\\\", "/"))
    while path.startswith("./"):
        path = path[2:]
    if path.startswith("/workspace/"):
        path = path[len("/workspace/"):]
    return path.rstrip("/")


def name_and_param_id(value):
    name = str(value or "").strip()
    if not name:
        return "", ""
    param_id = ""
    bracket = re.search(r"\\[([^\\]]*)\\]\\s*$", name)
    if bracket:
        param_id = bracket.group(1).strip()
        name = name[:bracket.start()].rstrip()
    if "(" in name:
        name = name.split("(", 1)[0].rstrip()
    spock = re.search(r"\\s+#([^\\s]+)\\s*$", name)
    if spock:
        param_id = param_id or spock.group(1).strip()
        name = name[:spock.start()].rstrip()
    return name, param_id


def canonical_identity(classname, name, file_path=None):
    normalized_name, param_id = name_and_param_id(name)
    if not normalized_name:
        return None
    normalized_class = str(classname or "").strip().replace("$", ".").strip(".")
    normalized_file = normalized_file_path(file_path)
    if normalized_file:
        module_or_file = normalized_file
    elif "." in normalized_class:
        module_or_file = normalized_class.rsplit(".", 1)[0]
    else:
        module_or_file = normalized_class
    class_name = normalized_class.rsplit(".", 1)[-1] if normalized_class else ""
    if not class_name and normalized_file:
        class_name = normalized_file.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return (module_or_file, class_name, normalized_name, param_id)


def identity_dict(identity):
    return {
        "module_or_file": identity[0],
        "class_name": identity[1],
        "name": identity[2],
        "param_id": identity[3],
    }


def display_name(identity):
    module_or_file, class_name, name, param_id = identity
    if "/" in module_or_file or module_or_file.endswith((".py", ".java")):
        owner = "::".join(part for part in (module_or_file, class_name) if part)
    else:
        owner = ".".join(part for part in (module_or_file, class_name) if part)
    prefix = "::".join(part for part in (owner, name) if part)
    return f"{prefix}[{param_id}]" if param_id else prefix


def testcase_status(testcase):
    child_names = {local_name(child) for child in testcase}
    if "error" in child_names:
        return "error"
    if "failure" in child_names:
        return "failed"
    if "skipped" in child_names or testcase.get("status") == "skipped":
        return "skipped"
    return "passed"


# Mirrors _collection_node_kind / _structured_error_line /
# _dominant_collection_error in the host module (Plan 4 Task 2): pytest
# collection nodes (empty classname + a "collection failure"/"collection
# skipped" child) never executed, so they are counted apart and never become
# canonical identities. Both parsers must agree or the compact path would keep
# reporting the audit's phantom "56 tests executed".
COLLECTION_EXCEPTION_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*\\s*:\\s+\\S")


def collection_node_kind(testcase):
    if (testcase.get("classname") or "").strip():
        return None
    for child in testcase:
        tag = local_name(child)
        if tag not in ("error", "skipped"):
            continue
        if "collection" in (child.get("message") or "").lower():
            return tag
    return None


def structured_error_line(testcase):
    error = None
    for child in testcase:
        if local_name(child) == "error":
            error = child
            break
    if error is None:
        return ""
    candidates = []
    for line in (error.text or "").splitlines():
        stripped = line.strip()
        if stripped != "E" and not stripped.startswith("E "):
            continue
        stripped = stripped[1:].strip().lstrip("|").strip()
        if COLLECTION_EXCEPTION_LINE.match(stripped):
            candidates.append(stripped)
    if candidates:
        return candidates[-1]
    return (error.get("message") or "").strip()


def dominant_collection_error(counts):
    dominant = None
    dominant_count = 0
    for message, count in counts.items():
        if message and count > dominant_count:
            dominant, dominant_count = message, count
    return dominant


def merge_status(current, new):
    severity = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}
    return new if severity.get(new, 0) > severity.get(current, 0) else current


def int_attr(node, name):
    try:
        return int(float(node.get(name, 0) or 0))
    except Exception:
        return 0


def content_sha256(path):
    """Current content hash, or None when the claimed report is gone."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return None


root = Path(project_dir)
# pytest --junitxml reports live OUTSIDE the project dir; scan that root too.
pytest_reports = Path(pytest_reports_dir)
scan_roots = [root] + ([pytest_reports] if pytest_reports.is_dir() else [])
scanned_files = sorted(
    {str(path) for scan_root in scan_roots for path in scan_root.rglob("*.xml") if is_report_file(path)}
)
# report_dirs stays a DISCOVERY fact over the whole scan: module coverage asks
# "which modules produced any reports at all", not "which are primary".
report_dirs = sorted({str(Path(path).parent) for path in scanned_files})

# The partition runs over whatever the receipts claim, INCLUDING nothing at
# all: an empty claim set makes every scanned report auxiliary.
verified = set()
unverified = set()
for claimed_path, claimed_hashes in receipt_claims.items():
    current = content_sha256(claimed_path)
    if current is None:
        # Claimed and since deleted: nothing on disk to attribute.
        continue
    if current in claimed_hashes:
        verified.add(claimed_path)
    else:
        unverified.add(claimed_path)
unverified -= verified
report_files = [path for path in scanned_files if path in verified]
auxiliary_files = [
    path for path in scanned_files if path not in verified and path not in unverified
]
stale_files = sorted(unverified)

groovy_classes = set()
for groovy in root.rglob("src/test/groovy/**/*.groovy"):
    try:
        text = groovy.read_text(errors="ignore")
    except Exception:
        continue
    if "@Test" in text:
        groovy_classes.add(groovy.stem)

parsing_errors = []
unmeasured_files = []
metrics_conflicts = set()


def parse_report(report_file, errors=None):
    """Return canonical cases, suite-only counts, and explicit attempt metadata.

    `errors` is the channel an unreadable report is reported through. It
    defaults to `parsing_errors`, the ATTRIBUTED channel: a receipt claimed
    those bytes, so the operator is told which claimed report the harness could
    not open. Excluded (auxiliary/stale) files pass their own list instead, so
    each destination's unreadable files are counted where they belong.

    No channel GRADES: an unreadable report has no measured volume to defend,
    and it can always be deleted (a claimed report that is gone attributes
    nothing and says nothing), so capping on one only ever paid a run for `rm`.
    """
    sink = parsing_errors if errors is None else errors
    try:
        tree = ET.parse(report_file)
        xml_root = tree.getroot()
    except Exception as exc:
        sink.append(f"Error parsing {report_file}: {exc}")
        return None
    attempt_values = set()
    attempt_error = None
    for element in xml_root.iter():
        if local_name(element) != "property" or element.get("name") != "sag.attempt_id":
            continue
        try:
            value = int(element.get("value") or "")
            if value < 1:
                raise ValueError
            attempt_values.add(value)
        except (TypeError, ValueError):
            attempt_error = "invalid"
    if len(attempt_values) > 1:
        attempt_error = "conflicting"
    attempt_id = (
        next(iter(attempt_values))
        if len(attempt_values) == 1 and not attempt_error
        else None
    )
    if attempt_id is None and attempt_error is None:
        attempt_error = "missing"

    testcases = [
        element for element in xml_root.iter() if local_name(element) == "testcase"
    ]
    if testcases:
        cases = []
        collection_errors = 0
        collection_errors_skipped = 0
        collection_messages = {}
        for testcase in testcases:
            classname = (testcase.get("classname") or "").strip()
            collection_kind = collection_node_kind(testcase)
            if collection_kind == "error":
                collection_errors += 1
                message = structured_error_line(testcase)
                if message:
                    collection_messages[message] = collection_messages.get(message, 0) + 1
                continue
            if collection_kind == "skipped":
                collection_errors_skipped += 1
                continue
            simple_classname = classname.split(".")[-1] if classname else ""
            if simple_classname in groovy_classes:
                continue
            identity = canonical_identity(
                classname,
                testcase.get("name"),
                testcase.get("file"),
            )
            if identity:
                cases.append((identity, testcase_status(testcase)))
        return {
            "cases": cases,
            "suite_counts": None,
            "attempt_id": attempt_id,
            "attempt_error": attempt_error,
            "collection_errors": collection_errors,
            "collection_errors_skipped": collection_errors_skipped,
            "collection_messages": collection_messages,
        }
    counts = {"total": 0, "failed": 0, "error": 0, "skipped": 0}
    suites = (
        [xml_root]
        if local_name(xml_root) == "testsuite"
        else [
            element for element in xml_root.iter() if local_name(element) == "testsuite"
        ]
    )
    for suite in suites:
        counts["total"] += int_attr(suite, "tests")
        counts["failed"] += int_attr(suite, "failures")
        counts["error"] += int_attr(suite, "errors")
        counts["skipped"] += int_attr(suite, "skipped")
    return {
        "cases": None,
        "suite_counts": counts,
        "attempt_id": attempt_id,
        "attempt_error": attempt_error,
        "collection_errors": 0,
        "collection_errors_skipped": 0,
        "collection_messages": {},
    }


def bump(counts, status):
    counts["total"] += 1
    if status == "error":
        counts["error"] += 1
    elif status == "failed":
        counts["failed"] += 1
    elif status == "skipped":
        counts["skipped"] += 1
    else:
        counts["passed"] += 1


def add_suite_counts(counts, suite_counts):
    counts["total"] += suite_counts["total"]
    counts["failed"] += suite_counts["failed"]
    counts["error"] += suite_counts["error"]
    counts["skipped"] += suite_counts["skipped"]
    counts["passed"] += max(
        0,
        suite_counts["total"]
        - suite_counts["failed"]
        - suite_counts["error"]
        - suite_counts["skipped"],
    )


# Aggregate all reports by canonical identity and explicit attempt. JVM report
# sets without SAG metadata are one external runner attempt (attempt 1); pytest
# reports are SAG-produced and therefore missing metadata is a metrics conflict.
raw = {"total": 0, "passed": 0, "failed": 0, "error": 0, "skipped": 0}
suite_only = {"total": 0, "passed": 0, "failed": 0, "error": 0, "skipped": 0}
attempts = {}
sources = {}
collection_errors_total = 0
collection_errors_skipped_total = 0
collection_messages_total = {}
for report_file in report_files:
    parsed = parse_report(report_file)
    if parsed is None:
        # The THIRD door out of the headline: a receipt claims these exact
        # bytes and the parser cannot open them. Not auxiliary (someone
        # claimed it), not stale (the claim still matches), and not volume
        # (nothing was measured) — so it is counted and pathed on its own.
        unmeasured_files.append(report_file)
        continue
    collection_errors_total += parsed["collection_errors"]
    collection_errors_skipped_total += parsed["collection_errors_skipped"]
    for message, count in parsed["collection_messages"].items():
        collection_messages_total[message] = collection_messages_total.get(message, 0) + count
    cases = parsed["cases"]
    suite_counts = parsed["suite_counts"]
    attempt_id = parsed["attempt_id"]
    attempt_error = parsed["attempt_error"]
    if is_pytest_report(report_file) and attempt_error:
        metrics_conflicts.add("test_attempt_id_invalid")
        parsing_errors.append(
            f"{attempt_error} sag.attempt_id in {report_file}; "
            "observations kept in attempt 1"
        )
    if attempt_id is None:
        attempt_id = 1
    if cases is None:
        add_suite_counts(raw, suite_counts)
        add_suite_counts(suite_only, suite_counts)
        if suite_counts["total"]:
            metrics_conflicts.add("test_identity_unavailable")
        continue
    for identity, status in cases:
        bump(raw, status)
        identity_attempts = attempts.setdefault(identity, {})
        identity_attempts[attempt_id] = (
            merge_status(identity_attempts[attempt_id], status)
            if attempt_id in identity_attempts
            else status
        )
        sources.setdefault(identity, set()).add(report_file)

latest = {"total": 0, "passed": 0, "failed": 0, "error": 0, "skipped": 0}
histories = []
failing_test_names = []
flaky_count = 0
retried_count = 0
for identity in sorted(attempts):
    identity_attempts = attempts[identity]
    attempt_ids = sorted(identity_attempts)
    statuses = [identity_attempts[attempt_id] for attempt_id in attempt_ids]
    first = statuses[0]
    current_latest = statuses[-1]
    worst = statuses[0]
    for status in statuses[1:]:
        worst = merge_status(worst, status)
    retries = max(len(attempt_ids) - 1, 0)
    flaky = current_latest == "passed" and worst in ("failed", "error")
    bump(latest, current_latest)
    flaky_count += int(flaky)
    retried_count += retries
    if current_latest in ("failed", "error"):
        failing_test_names.append(display_name(identity))
    histories.append(
        {
            "identity": identity_dict(identity),
            "first": first,
            "latest": current_latest,
            "worst": worst,
            "retried_count": retries,
            "attempt_ids": attempt_ids,
            "flaky": flaky,
            "sources": sorted(sources.get(identity, set())),
        }
    )

for key in latest:
    latest[key] += suite_only[key]

# Auxiliary and stale reports are aggregated on their own basis: raw statuses,
# no canonical identities, no histories. They are evidence ABOUT the run, never
# evidence OF the primary coordinate. Stale volume is counted for the same
# reason auxiliary volume is: a superseded-sha claim that names only a file path
# drops its executions out of every sentence, and a rewritten report then reads
# exactly like a report that never existed.
#
# Their parse failures are counted at their own door too. Sharing
# `parsing_errors` merged them into the attributed message list, so a stray XML
# no receipt vouches for was reported as a claimed report the run could not
# read. The count of what could not be read rides with the volume it belongs
# to, so every destination can say WHICH zero it is. No door grades: capping on
# an unreadable report only ever paid a run for deleting it (item 12).
def excluded_counts(files):
    counts = {"total": 0, "passed": 0, "failed": 0, "error": 0, "skipped": 0}
    unreadable = []
    for report_file in files:
        parsed = parse_report(report_file, unreadable)
        if parsed is None:
            continue
        if parsed["cases"] is None:
            add_suite_counts(counts, parsed["suite_counts"])
            continue
        for identity, status in parsed["cases"]:
            bump(counts, status)
    counts["unparseable"] = len(unreadable)
    return counts


auxiliary_counts = excluded_counts(auxiliary_files)
stale_counts = excluded_counts(stale_files)

result = {
    "valid": bool(scanned_files),
    "total_tests": latest["total"],
    "passed_tests": latest["passed"],
    "failed_tests": latest["failed"],
    "error_tests": latest["error"],
    "skipped_tests": latest["skipped"],
    "raw_total_tests": raw["total"],
    "raw_passed_tests": raw["passed"],
    "raw_failed_tests": raw["failed"],
    "raw_error_tests": raw["error"],
    "raw_skipped_tests": raw["skipped"],
    "unique_tests": latest["total"],
    "unique_passed_tests": latest["passed"],
    "unique_failed_tests": latest["failed"],
    "unique_error_tests": latest["error"],
    "unique_skipped_tests": latest["skipped"],
    "unique_methods": len({identity[:3] for identity in attempts}),
    "flaky_count": flaky_count,
    "retried_count": retried_count,
    "collection_errors": collection_errors_total,
    "collection_errors_skipped": collection_errors_skipped_total,
    "collection_error_summary": dominant_collection_error(collection_messages_total),
    "test_histories": histories,
    "metrics_conflicts": sorted(metrics_conflicts),
    "test_success": (
        latest["total"] > 0 and latest["failed"] == 0 and latest["error"] == 0
    ),
    "failing_test_names": sorted(failing_test_names)[:50],
    "report_files": report_files[:200],
    "report_file_count": len(report_files),
    "report_dirs": report_dirs,
    "parsing_errors": parsing_errors[:50],
}

# `receipt_scoped` is now CONSTANT for this parser: it states "these counts
# came out of the claim partition", which is every count this parser produces.
# The key is kept for schema stability (sealed snapshots, the report metrics
# projection and archived replay fixtures all read it), and its absence still
# carries meaning elsewhere — a rollup WITHOUT it did not come from here.
result["receipt_scoped"] = True
# Absent facts stay absent keys: nothing observed, nothing stated.
def excluded_stats(counts):
    stats = {
        "executed": counts["total"],
        "passed": counts["passed"],
        "failed": counts["failed"],
        "errors": counts["error"],
        "skipped": counts["skipped"],
    }
    # Absent facts stay absent keys, so a corpus that parsed cleanly seals the
    # exact block it always did.
    if counts["unparseable"]:
        stats["unparseable"] = counts["unparseable"]
    return stats


if auxiliary_files:
    result["auxiliary_test_stats"] = excluded_stats(auxiliary_counts)
    result["auxiliary_report_files"] = auxiliary_files[:200]
if stale_files:
    result["stale_test_reports"] = stale_files[:200]
    result["stale_test_stats"] = excluded_stats(stale_counts)
if unmeasured_files:
    # Attributed but unmeasured. The only fact here is HOW MANY claimed reports
    # could not be read: there is no executed/passed/... to state, and stating
    # zeros would be a measured outcome for bytes nobody measured.
    result["unmeasured_test_reports"] = unmeasured_files[:200]
    result["unmeasured_test_stats"] = {"unparseable": len(unmeasured_files)}
print(json.dumps(result, separators=(",", ":")))
'''


# --- pytest collection-node semantics (Plan 4 Task 2) -----------------------
# pytest's JUnit writer emits one testcase node per FILE it could not import,
# with an EMPTY classname and an `<error message="collection failure">` child
# (module-level skips come out as `<skipped message="collection skipped">`).
# Nothing executed in either case. The 2026-07-26 post-acceptance audit caught
# the harness reporting TVM's 28 + 28 collection nodes as "56 tests executed —
# 28 errors, 28 skipped"; the run had in fact never executed a single test.
# Collection nodes are therefore counted in their own stats and are excluded
# from total/passed/failed/errors/skipped and from canonical test identities.
#
# Detection signature (verified against all 58 recorded pytest artifacts under
# logs/: EVERY empty-classname node is one of these two shapes, and no other
# empty-classname node shape exists in any recorded run):
#   classname == "" AND child <error|skipped message contains "collection">
_COLLECTION_EXCEPTION_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*\s*:\s+\S")


def _collection_node_kind(testcase: ET.Element) -> Optional[str]:
    """Return "error"/"skipped" for a pytest collection node, else None."""
    if (testcase.get("classname") or "").strip():
        return None
    for tag in ("error", "skipped"):
        child = testcase.find(tag)
        if child is None:
            continue
        if "collection" in (child.get("message") or "").lower():
            return tag
    return None


def _structured_error_line(text: str, fallback: str = "") -> str:
    """The LAST `E   <Exception>: <msg>` line of a pytest error body.

    pytest prints the traceback plus one `E   ` line per raised exception; the
    last one is the cause that actually stopped collection (TVM's bodies show
    a swallowed `AttributeError` first and the real
    `RuntimeError: LLVM version is not available` last). Caret/underline `E `
    continuation lines are rejected by the identifier-colon shape.
    """
    candidates = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped != "E" and not stripped.startswith("E "):
            continue
        stripped = stripped[1:].strip().lstrip("|").strip()
        if _COLLECTION_EXCEPTION_LINE.match(stripped):
            candidates.append(stripped)
    if candidates:
        return candidates[-1]
    return (fallback or "").strip()


def _dominant_collection_error(counts: Dict[str, int]) -> Optional[str]:
    """Most frequent collection-error message; ties keep the first seen."""
    dominant = None
    dominant_count = 0
    for message, count in (counts or {}).items():
        if message and count > dominant_count:
            dominant, dominant_count = message, count
    return dominant


def _partition_collection_nodes(
    entries: List[Dict[str, any]],
) -> Tuple[List[Dict[str, any]], Dict[str, any]]:
    """Split runtime testcases from pytest collection nodes."""
    runtime_cases: List[Dict[str, any]] = []
    collection_errors = 0
    collection_errors_skipped = 0
    messages: Dict[str, int] = {}
    for entry in entries:
        kind = entry.get("collection_node")
        if kind == "error":
            collection_errors += 1
            message = entry.get("collection_message") or ""
            if message:
                messages[message] = messages.get(message, 0) + 1
            continue
        if kind == "skipped":
            collection_errors_skipped += 1
            continue
        runtime_cases.append(entry)
    return runtime_cases, {
        "collection_errors": collection_errors,
        "collection_errors_skipped": collection_errors_skipped,
        "collection_error_counts": messages,
        "collection_error_summary": _dominant_collection_error(messages),
    }


def _is_pytest_report_path(path: str, pytest_reports_dir: str) -> bool:
    """True for python_tool's per-invocation pytest XMLs.

    Mirrors the compact in-container parser's is_pytest_report so the shell
    find/cat fallback partitions report files on the same basis.
    """
    s = str(path)
    return (
        s.startswith(pytest_reports_dir.rstrip("/") + "/") or "/.setup_agent/pytest-reports/" in s
    )


def _test_report_attempt_id(xml_content: str) -> Tuple[Optional[int], Optional[str]]:
    """Read the explicit SAG attempt id without inferring transport order."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None, "malformed"

    values = set()
    invalid = False
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "property" or element.get("name") != "sag.attempt_id":
            continue
        try:
            value = int(element.get("value") or "")
            if value < 1:
                raise ValueError
            values.add(value)
        except (TypeError, ValueError):
            invalid = True

    if invalid:
        return None, "invalid"
    if len(values) > 1:
        return None, "conflicting"
    if not values:
        return None, "missing"
    return next(iter(values)), None


def _carries_unresolved_property(value: str) -> bool:
    """Whether a coordinate still holds an unexpanded `${...}` placeholder."""
    return "${" in str(value or "")


_GRADLE_INCLUDE_HEAD = re.compile(r"^include\b")
_GRADLE_QUOTED = re.compile(r"""(['"])([^'"\r\n]*)\1""")


def _parse_gradle_include_paths(settings_content: str) -> List[str]:
    """Subproject directories an `include` statement declares, root-relative.

    THE parse (P3): both the expected-artifact walk and the module scan read
    the settings file through here, so a subproject can never be a module to
    one and invisible to the other.

    One statement may name many subprojects across continuation lines — kafka's
    settings.gradle is a single `include` listing forty — so this walks the
    statement, not one regex match per `include` keyword. Both DSLs continue a
    statement the same way: a Groovy list broken after a comma, and a Kotlin
    ``include(`` call closed on a later line. ``includeBuild`` names a SEPARATE
    build in a composite, never a subproject of this one, and the word boundary
    after ``include`` is what refuses to match it.

    Returns ``':connect:api'`` as ``'connect/api'``: the directory Gradle maps a
    subproject to by default, which is where its ``build/classes`` lives.
    """
    paths: List[str] = []
    in_statement = False
    for raw in (settings_content or "").splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if _GRADLE_INCLUDE_HEAD.match(line):
            line = line[len("include") :]
        elif not in_statement:
            continue
        paths.extend(match.group(2) for match in _GRADLE_QUOTED.finditer(line))
        # A statement continues while the line ends open: a trailing comma
        # (Groovy list) or the opening paren of a multi-line Kotlin call.
        in_statement = line.endswith(",") or line.endswith("(")

    seen: List[str] = []
    for path in paths:
        rel = path.strip().strip(":").replace(":", "/").strip("/")
        if rel and rel not in seen:
            seen.append(rel)
    return seen


def _driven_test_modules_from_receipts(
    receipts: List[Mapping[str, Any]],
    *,
    test_modules: set[str],
) -> set[str]:
    """Survey roots with at least one complete module-qualified runtime row.

    ``domain_id`` is the exact surveyed root sealed on every row;
    ``module_coordinate`` proves the row was not merely attributed to a broad
    report directory.  An unavailable envelope contributes nothing.
    """

    driven: set[str] = set()
    for receipt in receipts:
        envelope = receipt.get("testcase_execution_rows")
        if not isinstance(envelope, Mapping) or envelope.get("status") != "complete":
            continue
        rows = envelope.get("rows")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            domain_id = str(row.get("domain_id") or "").strip()
            coordinate = str(row.get("module_coordinate") or "").strip()
            if domain_id in test_modules and coordinate:
                driven.add(domain_id)
    return driven


class TestEvidenceDecision(NamedTuple):
    """One decision: the label, the evidence word AND the sentence that says why.

    Spec 2026-08-14 §2.3. The three used to be selected by three separate
    branches over the same pass rate, which is how ignite sealed a "below the
    threshold" sentence beside a green evidence status. They are produced here or
    not at all, so a reason can never point away from the word it accompanies.
    """

    status: str
    evidence_status: str
    reason: str


def decide_test_evidence(
    *,
    valid: bool,
    executed: int,
    discovered: Optional[int],
    passed: int,
    failed: int,
    errors: int,
    skipped: int,
) -> TestEvidenceDecision:
    """Grade test evidence on EXECUTION, never on a pass percentage.

    The project's red is an exact fact the sentence states; it is not a SAG
    failure and it adjudicates nothing (spec §2.1/§2.2). Execution coverage is
    named by the rate bands downstream, not by a cut-off here.
    """

    if not valid:
        return TestEvidenceDecision("WARNING", "unknown", "No test reports found")
    if executed <= 0:
        return TestEvidenceDecision("FAILED", "blocked", no_execution_sentence(discovered))
    return TestEvidenceDecision(
        "SUCCESS",
        "success",
        execution_sentence(
            executed=executed,
            discovered=discovered,
            passed=passed,
            failed=failed,
            errors=errors,
            skipped=skipped,
        ),
    )


def _coverage_basis(coverage_info: Dict[str, Any]) -> str:
    """Does the CLASS-weighted coverage fraction have a basis? (Plan 8 §3.4)

    `derived` when class expectations were derived and the fraction is stated;
    `none` when they were not, which is why `class_coverage` is then absent
    rather than 1.0. It says nothing about the other expectations: a jar or file
    expectation is still a basis, and a met one still decides — "no class-based
    expectation" and "no expectation of any kind" are different facts.

    Stated by `_verify_expected_artifacts`; inferred from the expectation count
    for a caller that predates the field, so an old rollup still classifies the
    same way it always did rather than defaulting to "met".
    """
    stated = str((coverage_info or {}).get("basis") or "").strip()
    if stated in ("derived", "none"):
        return stated
    return "derived" if int((coverage_info or {}).get("classes_expected") or 0) > 0 else "none"


def _format_build_duration(seconds: float) -> str:
    """Human-friendly build duration.

    ``"47.2s"`` for under a minute (one decimal place); ``"3m 12s"`` otherwise,
    with the seconds component zero-padded to two digits.
    """
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}m {secs:02d}s"


class PhysicalValidator:
    """
    Validates build/test status based on physical evidence.

    This class checks actual files and re-executes commands to determine
    true status, eliminating inference-based errors.
    """

    def __init__(
        self,
        docker_orchestrator=None,
        project_path: str = "/workspace",
        compilation_recency_hours: int = 1,
        build_coverage_threshold: float = DEFAULT_BUILD_COVERAGE_THRESHOLD,
        command_tracker=None,
        receipt_run_id: Optional[str] = None,
    ):
        """
        Initialize physical validator.

        Args:
            docker_orchestrator: Docker orchestrator for command execution
            project_path: Base path of the project in container
            compilation_recency_hours: Hours to consider compilation as recent (default 1)
            build_coverage_threshold: Minimum source-weighted compiled-class coverage
                (fraction 0-1) for a multi-module build to count as green.
            command_tracker: Shared CommandTracker recording build/test commands
                and the build's wall-clock duration. validate_build_status reads
                the last recorded build off it to surface build_time/build_command
                in its evidence. May be attached after construction.
            receipt_run_id: Explicit current-run receipt epoch for isolated
                validation/tests. Production normally inherits the active
                invocation-receipt context established by SetupAgent.
        """
        self.docker_orchestrator = docker_orchestrator
        self.project_path = project_path
        self.compilation_recency_hours = compilation_recency_hours
        self.build_coverage_threshold = build_coverage_threshold
        self.command_tracker = command_tracker
        self.receipt_run_id = str(receipt_run_id or "").strip() or None

        # Cache for validation results with TTL
        self.validation_cache = {}
        self.cache_timestamps = {}
        self.cache_ttl = 60  # 60 seconds TTL
        self.last_validation = None

        logger.info(f"PhysicalValidator initialized for project at: {project_path}")

    def _get_cache_key(self, operation: str, *args) -> str:
        """Generate cache key for operation and arguments."""
        # Escape special characters in arguments to prevent cache key issues
        escaped_args = [quote(str(arg), safe="") for arg in args]
        return f"{operation}:{':'.join(escaped_args)}"

    def _is_cache_valid(self, cache_key: str) -> bool:
        """Check if cache entry is still valid (within TTL)."""
        if cache_key not in self.cache_timestamps:
            return False

        age = datetime.now().timestamp() - self.cache_timestamps[cache_key]
        return age < self.cache_ttl

    def _get_cached_result(self, cache_key: str):
        """Get cached result if valid, otherwise return None."""
        if self._is_cache_valid(cache_key):
            logger.debug(f"Cache hit for {cache_key}")
            return self.validation_cache[cache_key]
        else:
            # Clean up expired cache entry
            if cache_key in self.validation_cache:
                del self.validation_cache[cache_key]
            if cache_key in self.cache_timestamps:
                del self.cache_timestamps[cache_key]
            return None

    def _cache_result(self, cache_key: str, result):
        """Cache result with current timestamp."""
        self.validation_cache[cache_key] = result
        self.cache_timestamps[cache_key] = datetime.now().timestamp()
        logger.debug(f"Cached result for {cache_key}")

    def clear_cache(self):
        """Clear all cached results. Useful after build operations."""
        self.validation_cache.clear()
        self.cache_timestamps.clear()
        logger.debug("Cache cleared")

    def _execute_command_with_logging(
        self, command: str, operation: str = "command"
    ) -> Dict[str, any]:
        """
        Execute command with standardized result handling and logging.

        Args:
            command: Command to execute
            operation: Description of operation for logging

        Returns:
            Standardized result dictionary with success, output, exit_code
        """
        try:
            if not self.docker_orchestrator:
                return {
                    "success": False,
                    "output": "",
                    "exit_code": -1,
                    "error": "No docker orchestrator available",
                }

            result = self.docker_orchestrator.execute_command(command)

            # Standardize result format
            exit_code = result.get("exit_code", 1)  # Default to failure if not provided
            output = result.get("output", "")
            success = exit_code == 0

            # Log command execution details
            if success:
                logger.debug(f"✅ {operation} succeeded: {command[:100]}...")
            else:
                logger.warning(f"❌ {operation} failed (exit_code={exit_code}): {command[:100]}...")
                if output:
                    logger.warning(f"Command output: {output[:200]}...")

            return {
                "success": success,
                "output": output,
                "exit_code": exit_code,
                "command": command[:100] + "..." if len(command) > 100 else command,
            }

        except Exception as e:
            logger.error(f"❌ {operation} execution failed: {e}")
            logger.error(f"Command: {command[:100]}...")
            return {
                "success": False,
                "output": "",
                "exit_code": -1,
                "error": str(e),
                "command": command[:100] + "..." if len(command) > 100 else command,
            }

    def validate_build_artifacts(self, project_name: str = None) -> Dict[str, any]:
        """
        Check three levels of build evidence:
        1. .class files exist and are recent
        2. JAR files in target/build directories
        3. Compilation timestamps vs source timestamps

        Args:
            project_name: Name of the project (for path construction)

        Returns:
            Dictionary with validation results
        """
        if not self.docker_orchestrator:
            return {"valid": False, "error": "No docker orchestrator"}

        project_dir = f"{self.project_path}/{project_name}" if project_name else self.project_path

        # Use the complete artifact check to avoid duplication
        artifact_check = self._check_build_artifacts_complete(project_dir)

        validation_result = {
            "valid": False,
            "class_files": artifact_check["class_count"],
            "jar_files": artifact_check["jar_count"],
            "recent_compilation": False,
            "missing_classes": [],
            "evidence": [],
        }

        # Get sample class file paths for debugging
        class_check = self._check_class_files(project_dir)
        validation_result["class_file_paths"] = class_check["paths"][:10]  # Sample paths

        # Get JAR file paths
        jar_check = self._check_jar_files(project_dir)
        validation_result["jar_file_paths"] = jar_check["paths"]

        # Check compilation recency
        recency_check = self._check_compilation_recency(project_dir)
        validation_result["recent_compilation"] = recency_check["recent"]
        validation_result["newest_class_time"] = recency_check.get("newest_class_time")

        # Find missing classes (Java files without corresponding class files)
        missing = self.validate_missing_classes(project_dir)
        validation_result["missing_classes"] = missing

        # Determine overall validity
        validation_result["valid"] = (
            validation_result["class_files"] > 0
            and len(validation_result["missing_classes"]) == 0
            and validation_result["recent_compilation"]
        )

        # Build evidence summary
        if validation_result["valid"]:
            validation_result["evidence"].append(
                f"✅ Found {validation_result['class_files']} .class files"
            )
            validation_result["evidence"].append(
                f"✅ Found {validation_result['jar_files']} JAR files"
            )
            validation_result["evidence"].append("✅ Recent compilation detected")
        else:
            if validation_result["class_files"] == 0:
                validation_result["evidence"].append("❌ No .class files found")
            if len(validation_result["missing_classes"]) > 0:
                validation_result["evidence"].append(
                    f"❌ {len(validation_result['missing_classes'])} Java files not compiled"
                )
            if not validation_result["recent_compilation"]:
                validation_result["evidence"].append("❌ No recent compilation detected")

        self.last_validation = validation_result
        return validation_result

    def validate_build_artifacts_fresh(self, project_name: str = None) -> Dict[str, any]:
        """Report-time artifact validation that never serves a stale count.

        The mid-run artifact caches (``class_files`` / ``jar_files`` /
        ``artifacts_complete``, 60s TTL) exist so agent iterations don't hammer
        docker with repeated ``find`` scans. But at report generation those
        caches can hold a count taken minutes earlier — before later build/test
        phases compiled more classes. Live bigtop (session 20260713_014403): an
        early Maven module build cached 6 .class files, a later Gradle test
        compile brought the container to 162, and the report header still said
        "6 classes" while the per-module breakdown (which counts fresh via
        scan_modules) showed the Gradle modules as built.

        This helper clears the cache so the FINAL validation pass reflects the
        container's real state, then delegates to :meth:`validate_build_artifacts`.
        It is the report path's entry point ONLY — the agent-facing
        :meth:`validate_build_artifacts` keeps its mid-run caching untouched, so
        two consecutive checks within the TTL during the run still hit the cache.
        """
        self.clear_cache()
        return self.validate_build_artifacts(project_name=project_name)

    def _check_class_files(self, project_dir: str) -> Dict[str, any]:
        """Check for .class files in the project with caching."""
        cache_key = self._get_cache_key("class_files", project_dir)

        # Try cache first
        cached_result = self._get_cached_result(cache_key)
        if cached_result is not None:
            return cached_result

        try:
            # Count .class files
            count_cmd = f"find {project_dir} -name '*.class' -type f 2>/dev/null | wc -l"
            count_result = self._execute_command_with_logging(count_cmd, "class file count")

            if not count_result["success"]:
                logger.warning(
                    f"Class file count failed: {count_result.get('error', 'Unknown error')}"
                )
                return {"count": 0, "paths": [], "error": count_result.get("error")}

            count = int(count_result["output"].strip()) if count_result["output"].strip() else 0

            # Get all paths (removed head limit for complete scan)
            paths_cmd = f"find {project_dir} -name '*.class' -type f 2>/dev/null"
            paths_result = self._execute_command_with_logging(paths_cmd, "class file paths")

            paths = []
            if paths_result["success"] and paths_result["output"]:
                # Limit to first 100 for memory efficiency, but count is complete
                all_paths = [p.strip() for p in paths_result["output"].split("\n") if p.strip()]
                paths = all_paths[:100]  # Sample for details, but count is complete

            result = {"count": count, "paths": paths}

            # Cache the result
            self._cache_result(cache_key, result)
            return result
        except Exception as e:
            logger.error(f"Failed to check class files: {e}")
            return {"count": 0, "paths": [], "error": str(e)}

    def _check_jar_files(self, project_dir: str) -> Dict[str, any]:
        """Check for JAR files in target/build directories with caching."""
        cache_key = self._get_cache_key("jar_files", project_dir)

        # Try cache first
        cached_result = self._get_cached_result(cache_key)
        if cached_result is not None:
            return cached_result

        try:
            # Check Maven target and Gradle build directories (including build/libs)
            cmd = f"find {project_dir} \\( -path '*/target/*.jar' -o -path '*/build/*.jar' -o -path '*/build/libs/*.jar' \\) -type f 2>/dev/null"
            result = self._execute_command_with_logging(cmd, "JAR file search")

            paths = []
            if result["success"] and result["output"]:
                paths = [p.strip() for p in result["output"].split("\n") if p.strip()]
            elif not result["success"]:
                logger.warning(f"JAR file search failed: {result.get('error', 'Unknown error')}")

            result_dict = {"count": len(paths), "paths": paths}
            if not result["success"]:
                result_dict["error"] = result.get("error")

            # Cache the result
            self._cache_result(cache_key, result_dict)
            return result_dict
        except Exception as e:
            logger.error(f"Failed to check JAR files: {e}")
            return {"count": 0, "paths": [], "error": str(e)}

    def _check_compilation_recency(self, project_dir: str) -> Dict[str, any]:
        """Check if compilation is recent (within last hour)."""
        try:
            # Try GNU stat first (Linux/container environments) - get newest file
            cmd = f"find {project_dir} -name '*.class' -type f -exec stat -c '%Y' {{}} \\; 2>/dev/null | sort -rn | head -1"
            result = self._execute_command_with_logging(cmd, "GNU stat compilation check")

            if result["success"] and result["output"].strip():
                try:
                    newest_timestamp = int(result["output"].strip())
                    current_timestamp = int(datetime.now().timestamp())

                    # Check if compiled within last hour
                    age_seconds = current_timestamp - newest_timestamp
                    recent = age_seconds < (self.compilation_recency_hours * 3600)

                    return {
                        "recent": recent,
                        "newest_class_time": datetime.fromtimestamp(newest_timestamp).isoformat(),
                        "age_seconds": age_seconds,
                    }
                except (ValueError, OSError) as e:
                    logger.warning(f"Failed to parse GNU stat output: {e}")

            # Fallback to BSD stat (macOS) - get newest file
            logger.debug("GNU stat failed, trying BSD stat")
            cmd_bsd = f"find {project_dir} -name '*.class' -type f -exec stat -f '%m' {{}} \\; 2>/dev/null | sort -rn | head -1"
            result_bsd = self._execute_command_with_logging(cmd_bsd, "BSD stat compilation check")

            if result_bsd["success"] and result_bsd["output"].strip():
                try:
                    newest_timestamp = int(result_bsd["output"].strip())
                    current_timestamp = int(datetime.now().timestamp())

                    age_seconds = current_timestamp - newest_timestamp
                    recent = age_seconds < (self.compilation_recency_hours * 3600)

                    return {
                        "recent": recent,
                        "newest_class_time": datetime.fromtimestamp(newest_timestamp).isoformat(),
                        "age_seconds": age_seconds,
                    }
                except (ValueError, OSError) as e:
                    logger.warning(f"Failed to parse BSD stat output: {e}")

            # Final fallback to Python-based approach
            logger.debug("Both stat commands failed, using Python fallback")
            return self._check_compilation_recency_python_fallback(project_dir)

        except Exception as e:
            logger.error(f"Failed to check compilation recency: {e}")
            return {"recent": False, "error": str(e)}

    def _check_compilation_recency_python_fallback(self, project_dir: str) -> Dict[str, any]:
        """
        Python-based fallback for checking compilation recency.
        This method uses find + ls to get file modification times.
        """
        try:
            # Get list of .class files with detailed info (complete scan)
            cmd = f"find {project_dir} -name '*.class' -type f -exec ls -la {{}} \\; 2>/dev/null"
            result = self._execute_command_with_logging(cmd, "Python fallback compilation check")

            if not result["success"] or not result["output"].strip():
                return {"recent": False, "error": "No class files found or command failed"}

            newest_timestamp = 0
            current_timestamp = int(datetime.now().timestamp())

            # Parse ls output to extract modification times
            # ls -la format: -rw-r--r-- 1 user group size date time filename
            lines = result["output"].strip().split("\n")

            for line in lines:
                if not line.strip() or not line.startswith("-"):
                    continue

                try:
                    # Extract date/time from ls output
                    parts = line.split()
                    if len(parts) >= 8:
                        # Try to parse different date formats
                        date_str = f"{parts[5]} {parts[6]} {parts[7]}"

                        # Handle different ls date formats
                        try:
                            # Format: "Dec 25 14:30" (current year)
                            file_time = datetime.strptime(
                                f"{datetime.now().year} {date_str}", "%Y %b %d %H:%M"
                            )
                        except ValueError:
                            try:
                                # Format: "Dec 25 2023" (specific year)
                                file_time = datetime.strptime(date_str, "%b %d %Y")
                            except ValueError:
                                # Skip unparseable dates
                                continue

                        file_timestamp = int(file_time.timestamp())
                        if file_timestamp > newest_timestamp:
                            newest_timestamp = file_timestamp

                except (ValueError, IndexError) as e:
                    logger.debug(f"Failed to parse ls line '{line}': {e}")
                    continue

            if newest_timestamp > 0:
                age_seconds = current_timestamp - newest_timestamp
                recent = age_seconds < (self.compilation_recency_hours * 3600)

                return {
                    "recent": recent,
                    "newest_class_time": datetime.fromtimestamp(newest_timestamp).isoformat(),
                    "age_seconds": age_seconds,
                }

            return {"recent": False, "error": "Could not parse file timestamps"}

        except Exception as e:
            logger.error(f"Python fallback compilation recency check failed: {e}")
            return {"recent": False, "error": str(e)}

    def validate_missing_classes(self, project_dir: str) -> List[str]:
        """
        Find .java files without corresponding .class files.
        This indicates compilation failure.

        Args:
            project_dir: Project directory path

        Returns:
            List of Java files missing corresponding class files
        """
        try:
            # Get all Java source files
            java_cmd = f"find {project_dir}/src -name '*.java' -type f 2>/dev/null"
            java_result = self.docker_orchestrator.execute_command(java_cmd)
            java_files = [f.strip() for f in java_result.get("output", "").split("\n") if f.strip()]

            missing_classes = []

            # Check all files, not just first 100 (removed artificial limit)
            for java_file in java_files:
                # Extract expected class name from Java file path
                # e.g., /workspace/project/src/main/java/com/example/MyClass.java
                # -> com/example/MyClass.class

                # Get the relative path from src/main/java or src/test/java
                relative_path = None
                for src_pattern in ["/src/main/java/", "/src/test/java/"]:
                    if src_pattern in java_file:
                        relative_path = java_file.split(src_pattern)[1]
                        break

                if relative_path:
                    # Convert to class path
                    class_name = relative_path.replace(".java", ".class")

                    # Check if corresponding class file exists
                    class_check_cmd = (
                        f"find {project_dir} -path '*/{class_name}' -type f 2>/dev/null | head -1"
                    )
                    class_result = self.docker_orchestrator.execute_command(class_check_cmd)

                    if not class_result.get("output", "").strip():
                        missing_classes.append(java_file)
                        # Removed limit - check all missing classes for accuracy

            return missing_classes
        except Exception as e:
            logger.error(f"Failed to validate missing classes: {e}")
            return []

    def _extract_test_statistics(self, command: str, output: str) -> Dict[str, int]:
        """
        Extract test statistics from command output.

        Args:
            command: The test command executed
            output: Command output

        Returns:
            Dictionary with test statistics
        """
        stats = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}

        if "mvn" in command or "maven" in command.lower():
            # Maven test output pattern
            pattern = (
                r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)"
            )
            matches = re.findall(pattern, output)

            for match in matches:
                total = int(match[0])
                failures = int(match[1])
                errors = int(match[2])
                skipped = int(match[3])

                stats["total"] += total
                stats["failed"] += failures + errors
                stats["skipped"] += skipped
                stats["passed"] += total - failures - errors - skipped

        elif "gradle" in command:
            # Gradle test output pattern
            pattern = r"(\d+)\s+tests?\s+completed,\s*(\d+)\s+failed"
            match = re.search(pattern, output)

            if match:
                total = int(match.group(1))
                failed = int(match.group(2))

                stats["total"] = total
                stats["failed"] = failed
                stats["passed"] = total - failed

        return stats

    def parse_test_reports(
        self, project_dir: str, test_catalog: Optional[TestCaseCatalog] = None
    ) -> Dict[str, any]:
        """
        Parse test report XML files to get accurate test statistics.
        Supports both Maven Surefire and Gradle test reports.

        Args:
            project_dir: Project directory path
            test_catalog: Optional pre-built catalog from static analysis

        Returns:
            Dictionary with detailed test statistics and status
        """
        if not self.docker_orchestrator:
            return {"valid": False, "error": "No docker orchestrator"}

        cache_key = self._get_cache_key("test_reports", project_dir)

        # Try cache first
        cached_result = self._get_cached_result(cache_key)
        if cached_result is not None:
            return cached_result

        test_result = {
            "valid": False,
            "total_tests": 0,
            "raw_total_tests": 0,
            "unique_tests": 0,
            "passed_tests": 0,
            "raw_passed_tests": 0,
            "failed_tests": 0,
            "raw_failed_tests": 0,
            "error_tests": 0,
            "raw_error_tests": 0,
            "skipped_tests": 0,
            "raw_skipped_tests": 0,
            "unique_passed_tests": 0,
            "unique_failed_tests": 0,
            "unique_error_tests": 0,
            "unique_skipped_tests": 0,
            "unique_methods": 0,
            "flaky_count": 0,
            "retried_count": 0,
            # pytest collection nodes: counted, never executed (Plan 4 Task 2).
            "collection_errors": 0,
            "collection_errors_skipped": 0,
            "collection_error_summary": None,
            "test_histories": [],
            "metrics_conflicts": [],
            "test_success": False,
            "failing_test_names": [],
            "report_files": [],
            "report_file_count": 0,
            "report_dirs": [],
            "parsing_errors": [],
        }

        # Receipt scoping crosses the host-publication boundary exactly once.
        # A malformed, unpublished, deleted or rolled-back ledger is not the
        # fact "no receipts" and therefore cannot restore a legacy global
        # scan. The compact parser receives only this verified snapshot.
        receipt_records = self._read_live_invocation_receipts()
        if receipt_records is None:
            return self._receipt_evidence_failure(
                test_result,
                cache_key,
                "invocation receipt ledger is not host-authorized and complete",
                [],
            )
        receipts_present = bool(receipt_records)
        primary_root = self._primary_test_coordinate_root() if receipts_present else None
        resolution = getattr(self, "_last_test_candidate_resolution", None)
        test_modules = {
            str(candidate.root)
            for candidate in getattr(resolution, "candidates", ())
            if str(getattr(candidate, "root", "")).strip()
        }
        coordinate_unresolved = receipts_present and not primary_root

        try:
            compact_result = self._parse_test_reports_compact_in_container(
                project_dir,
                primary_root=primary_root,
                receipt_records=receipt_records,
            )
            if compact_result and compact_result.get("receipt_error"):
                return self._receipt_evidence_failure(
                    test_result,
                    cache_key,
                    compact_result["receipt_error"],
                    compact_result.get("receipt_error_files") or [],
                )
            if receipts_present and compact_result is None:
                # Receipts exist but the receipt-aware parser never ran: an
                # unscoped global rollup would silently re-inflate the primary
                # numerator, so refuse it instead.
                return self._receipt_evidence_failure(
                    test_result,
                    cache_key,
                    (
                        "invocation receipts exist but the receipt-scoped report "
                        f"parser could not run in the container ({self._invocation_receipts_dir()})"
                    ),
                    [],
                )
            if compact_result and compact_result.get("valid"):
                test_result.update(compact_result)
                if test_modules:
                    test_result["test_modules"] = sorted(test_modules)
                    test_result["driven_modules"] = sorted(
                        _driven_test_modules_from_receipts(
                            receipt_records,
                            test_modules=test_modules,
                        )
                    )
                test_result.setdefault("report_files", [])
                test_result.setdefault("report_dirs", [])
                test_result.setdefault(
                    "report_file_count", len(test_result.get("report_files") or [])
                )
                test_result.setdefault("collection_errors", 0)
                test_result.setdefault("collection_errors_skipped", 0)
                test_result.setdefault("collection_error_summary", None)
                if coordinate_unresolved:
                    test_result["metrics_conflicts"] = sorted(
                        {
                            *(test_result.get("metrics_conflicts") or ()),
                            "test_primary_coordinate_unresolved",
                        }
                    )

                modules_without_tests = self._check_modules_without_tests(
                    project_dir, test_result.get("report_dirs") or []
                )
                if modules_without_tests:
                    test_result["modules_without_tests"] = modules_without_tests
                    logger.warning(
                        f"⚠️ Multi-module project: {len(modules_without_tests)} modules lack test reports: "
                        f"{', '.join(modules_without_tests[:5])}"
                        f"{'...' if len(modules_without_tests) > 5 else ''}"
                    )

                self._cache_result(cache_key, test_result)
                return test_result

            if receipts_present:
                # The receipt-scoped parser ran and found no report XML at all.
                # The shell rescan below is deliberately skipped: its broader,
                # provenance-free discovery is exactly what receipts exist to
                # replace, so it must never become the receipt-scoped answer.
                test_result["error"] = "No test report files found"
                self._cache_result(cache_key, test_result)
                return test_result

            # Step 1: Discover report directories first to avoid massive single-command outputs
            # Include Maven Surefire, Maven Failsafe, and Gradle test-results
            dirs_cmd = (
                f"find {project_dir} -type d "
                f"\\( -name 'surefire-reports' -o -name 'failsafe-reports' -o -path '*/build/test-results/*' \\) 2>/dev/null"
            )
            dirs_result = self.docker_orchestrator.execute_command(dirs_cmd)
            report_dirs = []
            if dirs_result.get("exit_code") == 0 and dirs_result.get("output"):
                report_dirs = [
                    d.strip() for d in dirs_result.get("output", "").split("\n") if d.strip()
                ]

            # Python: python_tool writes pytest --junitxml reports OUTSIDE the
            # project dir (PYTEST_REPORT_DIR). The XML inside is standard JUnit
            # XML, so this parser consumes it UNCHANGED (spec 2026-07-07
            # Component 4) — only the discovery gains the extra directory.
            from sag.tools.internal.python_tool import PYTEST_REPORT_DIR

            pytest_probe = self.docker_orchestrator.execute_command(
                f"test -d {PYTEST_REPORT_DIR} && echo EXISTS"
            )
            if "EXISTS" in (pytest_probe.get("output") or ""):
                report_dirs.append(PYTEST_REPORT_DIR)

            # Step 2: For each directory, list XML files in small batches to avoid truncation
            report_files: List[str] = []
            for report_dir in report_dirs:
                # Limit depth to keep per-command output small; Gradle may have nested per-class dirs
                list_cmd = f"find '{report_dir}' -maxdepth 2 -type f -name '*.xml' 2>/dev/null"
                list_result = self.docker_orchestrator.execute_command(list_cmd)
                if list_result.get("exit_code") == 0 and list_result.get("output"):
                    files = [
                        f.strip() for f in list_result.get("output", "").split("\n") if f.strip()
                    ]
                    # Filter obviously irrelevant XMLs if any (keep flexible)
                    report_files.extend(files)

            # Fallback: If directory discovery failed to find anything, do a global file search (may be heavy)
            if not report_files:
                fallback_cmd = f"find {project_dir} -type f \\( -path '*/surefire-reports/*.xml' -o -path '*/failsafe-reports/*.xml' -o -path '*/test-results/*.xml' \\) 2>/dev/null"
                fallback_res = self.docker_orchestrator.execute_command(fallback_cmd)
                if fallback_res.get("exit_code") == 0 and fallback_res.get("output"):
                    report_files = [
                        f.strip() for f in fallback_res.get("output", "").split("\n") if f.strip()
                    ]

            test_result["report_files"] = report_files

            if not report_files:
                test_result["error"] = "No test report files found"
                return test_result

            logger.info(f"📊 Processing {len(report_files)} test report XML files...")

            # Build catalog if not provided
            if test_catalog is None and project_dir.startswith("/workspace"):
                logger.debug("Building test catalog for comparison...")
                test_catalog = build_java_test_catalog(project_dir, self.docker_orchestrator)

            # Step 3: Parse all XML files
            # Track runtime test cases with full metadata
            test_case_records: Dict[str, RuntimeTestCaseRecord] = {}

            # Debug: track processing
            files_with_testcases = 0
            total_testcases_found = 0

            observations: List[TestResultObservation] = []
            identity_statuses: Dict[CanonicalTestIdentity, List[str]] = {}
            identity_times: Dict[CanonicalTestIdentity, float] = {}
            identity_raw_names: Dict[CanonicalTestIdentity, set] = {}
            identity_catalog_keys: Dict[CanonicalTestIdentity, str] = {}
            runtime_catalog_keys = set()
            metrics_conflicts = set()
            suite_only = {
                "executed": 0,
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
            }
            collection_errors = 0
            collection_errors_skipped = 0
            collection_error_counts: Dict[str, int] = {}

            for report_file in report_files:
                try:
                    xml_result = self.docker_orchestrator.execute_command(f"cat '{report_file}'")
                    if xml_result.get("exit_code") != 0:
                        test_result["parsing_errors"].append(f"Failed to read {report_file}")
                        continue

                    xml_content = xml_result.get("output", "")
                    if not xml_content.strip():
                        continue
                    stats = self._parse_single_test_xml(xml_content, report_file)
                    if not stats:
                        test_result["parsing_errors"].append(
                            f"Failed to parse XML structure in {report_file}"
                        )
                        continue

                    collection_errors += stats.get("collection_errors", 0) or 0
                    collection_errors_skipped += stats.get("collection_errors_skipped", 0) or 0
                    for message, count in (stats.get("collection_error_counts") or {}).items():
                        collection_error_counts[message] = (
                            collection_error_counts.get(message, 0) + count
                        )

                    attempt_id, attempt_error = _test_report_attempt_id(xml_content)
                    is_pytest = _is_pytest_report_path(report_file, PYTEST_REPORT_DIR)
                    if (is_pytest and attempt_error) or (attempt_error not in (None, "missing")):
                        metrics_conflicts.add("test_attempt_id_invalid")
                        test_result["parsing_errors"].append(
                            f"{attempt_error} sag.attempt_id in {report_file}; "
                            "observations kept in attempt 1"
                        )
                    attempt_id = attempt_id or 1

                    testcases_in_file = stats.get("testcases", [])
                    if not testcases_in_file:
                        suite_only["executed"] += stats.get("total", 0)
                        suite_only["passed"] += stats.get("passed", 0)
                        suite_only["failed"] += stats.get("failed", 0)
                        suite_only["errors"] += stats.get("errors", 0)
                        suite_only["skipped"] += stats.get("skipped", 0)
                        if stats.get("total", 0):
                            metrics_conflicts.add("test_identity_unavailable")
                        continue

                    files_with_testcases += 1
                    total_testcases_found += len(testcases_in_file)
                    for testcase in testcases_in_file:
                        identity = canonical_test_identity(
                            testcase.get("classname", ""),
                            testcase.get("name"),
                            testcase.get("identity_file"),
                        )
                        if not identity:
                            continue
                        status = testcase.get("status", "passed")
                        observations.append(
                            TestResultObservation(
                                identity=identity,
                                attempt_id=attempt_id,
                                status=status,
                                source=report_file,
                            )
                        )
                        identity_statuses.setdefault(identity, []).append(status)
                        identity_times[identity] = identity_times.get(identity, 0.0) + (
                            float(testcase.get("time") or 0.0) * 1000
                        )
                        identity_raw_names.setdefault(identity, set()).add(
                            testcase.get("name") or ""
                        )
                        catalog_key = normalize_testcase_identifier(
                            testcase.get("classname", ""),
                            testcase.get("name"),
                            testcase.get("identity_file"),
                        )
                        if catalog_key:
                            runtime_catalog_keys.add(catalog_key)
                            identity_catalog_keys.setdefault(identity, catalog_key)
                except Exception as exc:
                    test_result["parsing_errors"].append(f"Error parsing {report_file}: {exc}")
                    logger.warning(f"Failed to parse test report {report_file}: {exc}")

            aggregated = aggregate_test_results(observations)
            latest = dict(aggregated.latest_counts)
            raw = dict(aggregated.raw_counts)
            for key in latest:
                latest[key] += suite_only[key]
                raw[key] += suite_only[key]

            for identity, history in aggregated.histories.items():
                display_key = identity.display_name
                catalog_key = identity_catalog_keys.get(identity)
                descriptor = test_catalog.get(catalog_key) if test_catalog and catalog_key else None
                test_case_records[display_key] = RuntimeTestCaseRecord(
                    descriptor=descriptor,
                    key=display_key,
                    statuses=identity_statuses.get(identity, []),
                    final_status=history.latest,
                    execution_time_ms=identity_times.get(identity, 0.0),
                    sources=set(history.sources),
                    raw_names=sorted(identity_raw_names.get(identity, set())),
                )

            test_result.update(
                {
                    "total_tests": latest["executed"],
                    "passed_tests": latest["passed"],
                    "failed_tests": latest["failed"],
                    "error_tests": latest["errors"],
                    "skipped_tests": latest["skipped"],
                    "raw_total_tests": raw["executed"],
                    "raw_passed_tests": raw["passed"],
                    "raw_failed_tests": raw["failed"],
                    "raw_error_tests": raw["errors"],
                    "raw_skipped_tests": raw["skipped"],
                    "unique_tests": latest["executed"],
                    "unique_passed_tests": latest["passed"],
                    "unique_failed_tests": latest["failed"],
                    "unique_error_tests": latest["errors"],
                    "unique_skipped_tests": latest["skipped"],
                    "unique_methods": len(
                        {
                            (
                                identity.module_or_file,
                                identity.class_name,
                                identity.name,
                            )
                            for identity in aggregated.histories
                        }
                    ),
                    "flaky_count": aggregated.flaky_count,
                    "retried_count": aggregated.retried_count,
                    "collection_errors": collection_errors,
                    "collection_errors_skipped": collection_errors_skipped,
                    "collection_error_summary": _dominant_collection_error(collection_error_counts),
                    "test_histories": aggregated.to_dict()["histories"],
                    "metrics_conflicts": sorted(metrics_conflicts),
                    "failing_test_names": sorted(
                        identity.display_name
                        for identity, history in aggregated.histories.items()
                        if history.latest in ("failed", "error")
                    )[:50],
                    "test_case_records": {
                        key: record.to_dict() for key, record in test_case_records.items()
                    },
                }
            )

            logger.info(
                f"📊 Debug: Found {total_testcases_found} total testcases "
                f"in {files_with_testcases} files"
            )
            logger.info(
                f"📊 Collected {test_result['unique_tests']} canonical test cases "
                "from all XML files"
            )
            if test_result["unique_tests"]:
                logger.info(
                    "📊 Test deduplication complete: "
                    f"{test_result['raw_total_tests']} raw -> "
                    f"{test_result['unique_tests']} canonical latest"
                )

            # Detect unexecuted tests by comparing the static catalog with the
            # legacy method keys observed alongside canonical runtime identities.
            if test_catalog:
                unexecuted_tests = []
                catalog_keys = set(test_catalog.get_all().keys())
                for key in sorted(catalog_keys - runtime_catalog_keys):
                    descriptor = test_catalog.get(key)
                    if descriptor:
                        unexecuted_tests.append(
                            {
                                "key": key,
                                "package": descriptor.package,
                                "class": descriptor.class_name,
                                "method": descriptor.method_name,
                                "path": descriptor.file_path,
                                "module": descriptor.module,
                            }
                        )

                if unexecuted_tests:
                    test_result["unexecuted_tests"] = unexecuted_tests
                    test_result["unexecuted_count"] = len(unexecuted_tests)
                    logger.warning(f"⚠️ Found {len(unexecuted_tests)} tests that were not executed")
                    self._save_unexecuted_tests_log(project_dir, unexecuted_tests, test_catalog)

            # Determine test success: only if no failures and no errors
            test_result["test_success"] = (
                test_result["failed_tests"] == 0
                and test_result["error_tests"] == 0
                and test_result["total_tests"] > 0
            )
            test_result["valid"] = True

            logger.info(
                f"📊 Test report analysis: {test_result['total_tests']} total, "
                f"{test_result['passed_tests']} passed, {test_result['failed_tests']} failed, "
                f"{test_result['error_tests']} errors, {test_result['skipped_tests']} skipped"
            )

            # Check for multi-module projects and identify modules without tests
            modules_without_tests = self._check_modules_without_tests(project_dir, report_dirs)
            if modules_without_tests:
                test_result["modules_without_tests"] = modules_without_tests
                logger.warning(
                    f"⚠️ Multi-module project: {len(modules_without_tests)} modules lack test reports: "
                    f"{', '.join(modules_without_tests[:5])}"
                    f"{'...' if len(modules_without_tests) > 5 else ''}"
                )

        except Exception as e:
            test_result["error"] = f"Failed to parse test reports: {str(e)}"
            logger.error(f"Test report parsing failed: {e}")

        # Cache the result
        self._cache_result(cache_key, test_result)
        return test_result

    # --- invocation receipts (Plan 5 Task B2) ------------------------------
    def _invocation_receipts_dir(self) -> str:
        """The invocation receipt directory for this workspace."""
        return f"{self.project_path.rstrip('/')}/{INVOCATION_RECEIPTS_DIRNAME}"

    def _evidence_assessments_dir(self) -> str:
        """The assessment sibling of this workspace's receipt directory."""

        return posixpath.join(
            posixpath.dirname(self._invocation_receipts_dir()),
            "evidence_assessments",
        )

    def _read_live_invocation_receipts(self) -> Optional[List[Dict[str, Any]]]:
        """Return one complete host-published current receipt snapshot.

        ``[]`` is a host-verified empty current set. ``None`` means the named
        stream, strict v2 schema/filename binding, exact bytes, contract tuple
        or immutable expected-id set failed. Historical v1 and fully verified
        foreign-run v2 records remain forensic and never enter this snapshot.
        """

        if not getattr(self, "docker_orchestrator", None):
            return []
        from sag.agent.invocation_receipts import (
            receipt_record_scope,
            validate_receipt_v2,
        )

        read = read_live_published_json_records(
            self.docker_orchestrator,
            self._invocation_receipts_dir(),
            record_kind="invocation_receipt",
            validator=lambda payload, expected_id: validate_receipt_v2(
                payload,
                expected_id=expected_id,
            ),
            publication_binding=lambda payload: EvidencePublicationBinding(
                run_id=payload["run_id"],
                contract_id=payload.get("contract_id"),
                contract_hash=payload.get("contract_hash"),
            ),
            record_scope=receipt_record_scope,
        )
        if not read.complete or read.conflict is not None:
            logger.debug(
                "physical receipt ledger unavailable: "
                f"{read.conflict or read.detail or 'incomplete'}"
            )
            return None
        return [dict(record.payload) for record in read.records]

    @staticmethod
    def _receipt_sequence(receipt: Mapping[str, Any]) -> tuple[int, str]:
        receipt_id = str(receipt.get("receipt_id") or "").strip()
        try:
            sequence = int(receipt_id.rsplit("-", 1)[-1])
        except ValueError:
            sequence = -1
        return sequence, receipt_id

    def _current_scoped_receipts(
        self,
        project_dir: str,
    ) -> Optional[List[Dict[str, Any]]]:
        """Current-run receipts bound to this checkout and project root."""

        records = self._read_live_invocation_receipts()
        if records is None:
            return None
        if not records:
            return []
        from sag.agent.attempt_policy import (
            current_run_durable_receipt,
            resolve_current_build_receipt_scope,
        )
        from sag.agent.invocation_receipts import active_receipt_run_id

        run_id = self.receipt_run_id or active_receipt_run_id()
        scope = resolve_current_build_receipt_scope(
            self.docker_orchestrator,
            run_id=run_id,
            workspace_root=self.project_path,
            project_root=project_dir,
        )
        if not scope.available:
            return None
        return [
            receipt
            for receipt in records
            if current_run_durable_receipt(
                receipt,
                receipt_id=str(receipt.get("receipt_id") or ""),
                run_id=run_id,
                target_sha=scope.target_sha or "",
                project_root=scope.project_root or project_dir,
            )
        ]

    def _terminal_root_maven_reactor_receipt(
        self,
        project_dir: str,
    ) -> Optional[Dict[str, Any]]:
        """Newest root Maven build receipt when it proves complete success.

        This is execution authority, not a heuristic source/class census. A
        scoped reactor, a non-exact contract, a later failed build, or a
        partial module summary cannot satisfy it.
        """

        receipts = self._current_scoped_receipts(project_dir)
        if not receipts:
            return None
        build_actions = {"compile", "package", "install"}
        candidates = [
            receipt
            for receipt in receipts
            if str(receipt.get("tool") or "").strip().lower() == "maven"
            and str(receipt.get("requested_action") or "").strip().lower() in build_actions
            and posixpath.normpath(
                str(receipt.get("actual_cwd") or receipt.get("working_directory") or "")
            )
            == posixpath.normpath(project_dir)
        ]
        if not candidates:
            return None
        latest = max(candidates, key=self._receipt_sequence)
        outcomes = latest.get("module_outcomes")
        if (
            latest.get("exit_code") != 0
            or str(latest.get("outcome") or "").strip().lower() != "completed"
            or not _dispatch_terminated(latest)
            or str(latest.get("compliance") or "").strip().lower() != "exact"
            or not isinstance(outcomes, list)
            or not outcomes
            or any(
                str((entry or {}).get("status") or "").strip().lower() != "success"
                for entry in outcomes
            )
        ):
            return None
        return {
            "receipt_id": str(latest.get("receipt_id") or ""),
            "modules_succeeded": len(outcomes),
            "modules_total": len(outcomes),
            "requested_action": str(latest.get("requested_action") or ""),
        }

    def _test_execution_receipt_summary(self, project_dir: str) -> Dict[str, Any]:
        """Separate runner completion from the outcomes in emitted test rows."""

        receipts = self._current_scoped_receipts(project_dir)
        if receipts is None:
            return {"state": "unknown", "reason": "test receipt scope unavailable"}
        test_actions = {"test", "verify", "integration-test"}
        candidates = []
        for receipt in receipts:
            requested = str(receipt.get("requested_action") or "").strip().lower()
            effective = str(receipt.get("effective_action") or "").strip().lower()
            if (
                requested not in test_actions
                and effective not in test_actions
                and not isinstance(receipt.get("testcase_execution_rows"), Mapping)
            ):
                continue
            candidates.append(receipt)
        if not candidates:
            return {"state": "unknown", "reason": "no current test receipt"}

        latest_by_action: Dict[tuple[str, str, str], Mapping[str, Any]] = {}
        for receipt in sorted(candidates, key=self._receipt_sequence):
            key = (
                str(receipt.get("tool") or "").strip().lower(),
                str(receipt.get("requested_action") or receipt.get("effective_action") or "")
                .strip()
                .lower(),
                posixpath.normpath(
                    str(
                        receipt.get("actual_cwd") or receipt.get("working_directory") or project_dir
                    )
                ),
            )
            latest_by_action[key] = receipt

        interrupted: List[str] = []
        completed: List[str] = []
        observed_rows = 0
        for receipt in latest_by_action.values():
            receipt_id = str(receipt.get("receipt_id") or "").strip()
            envelope = receipt.get("testcase_execution_rows")
            rows = envelope.get("rows") if isinstance(envelope, Mapping) else None
            if isinstance(rows, list):
                observed_rows += len(rows)
            exit_code = receipt.get("exit_code")
            lifecycle = str(receipt.get("lifecycle_state") or "").strip().lower()
            was_interrupted = bool(
                str(receipt.get("termination_reason") or "").strip()
                or lifecycle == "vanished"
                or (
                    isinstance(exit_code, int)
                    and not isinstance(exit_code, bool)
                    and exit_code >= 128
                )
            )
            if was_interrupted:
                interrupted.append(receipt_id)
            elif isinstance(exit_code, int) and not isinstance(exit_code, bool):
                completed.append(receipt_id)

        if interrupted:
            state = "partial" if observed_rows else "failed"
            return {
                "state": state,
                "reason": (
                    f"test execution was interrupted after {observed_rows:,} sealed row(s)"
                    if observed_rows
                    else "test execution was interrupted before any sealed test row"
                ),
                "receipt_ids": [
                    str(receipt.get("receipt_id") or "") for receipt in latest_by_action.values()
                ],
                "interrupted_receipt_ids": interrupted,
                "observed_rows": observed_rows,
            }
        if completed and len(completed) == len(latest_by_action):
            return {
                "state": "completed",
                "reason": "test runner receipts reached terminal process outcomes",
                "receipt_ids": completed,
                "observed_rows": observed_rows,
            }
        return {
            "state": "unknown",
            "reason": "test receipt completion is unavailable",
            "receipt_ids": [
                str(receipt.get("receipt_id") or "") for receipt in latest_by_action.values()
            ],
            "observed_rows": observed_rows,
        }

    def _read_live_evidence_assessments(self) -> Optional[List[Dict[str, Any]]]:
        """Return the complete strict host-published assessment union."""

        if not self.docker_orchestrator:
            return []
        from sag.agent.evidence_assessments import validate_assessment_v2

        read = read_live_published_json_records(
            self.docker_orchestrator,
            self._evidence_assessments_dir(),
            record_kind="receipt_assessment",
            validator=lambda payload, expected_id: validate_assessment_v2(
                payload,
                expected_id=expected_id,
            ),
        )
        if not read.complete or read.conflict is not None:
            logger.debug(
                "physical assessment ledger unavailable: "
                f"{read.conflict or read.detail or 'incomplete'}"
            )
            return None
        return [dict(record.payload) for record in read.records]

    @staticmethod
    def _verified_report_claims(
        receipt_records: List[Mapping[str, Any]],
        primary_root: Optional[str],
    ) -> Dict[str, List[str]]:
        """Project a verified receipt snapshot to report path/hash claims."""

        claims: Dict[str, set[str]] = {}
        primary_prefix = str(primary_root or "").rstrip("/")
        for payload in receipt_records:
            working_directory = str(payload.get("working_directory") or "").rstrip("/") or "/"
            if primary_prefix and (
                working_directory != primary_prefix
                and not working_directory.startswith(primary_prefix + "/")
            ):
                continue
            delta = payload.get("report_delta") or {}
            for bucket in ("new", "changed", "cached"):
                for entry in delta.get(bucket) or ():
                    claims.setdefault(str(entry["path"]), set()).add(
                        str(entry["sha256"]).strip().lower()
                    )
        return {path: sorted(digests) for path, digests in sorted(claims.items())}

    def _attempted_module_evidence(
        self,
        project_dir: Optional[str] = None,
    ) -> "_AttemptedModules":
        """Modules THIS run's dispatches attempted, and whether we may narrow.

        Read from current-run, target/domain-bound production build receipts'
        `module_outcomes` — Maven's reactor summary, the modules whose tasks
        Gradle ran. This is the coverage SCOPE: counting
        `src/main/java` across the whole tree measures modules a scoped build
        (`-pl`), an early-stopping reactor or a disabled profile never tried, and
        calls them missing.

        THE ONE RULE (spec §2 P4, §3.6): a receipt the harness will not trust as
        a PROVER is still counted as a CLAIMANT. Every module any readable
        receipt named goes into `modules`, because they are modules this run
        attempted; what a receipt's untrustworthiness costs is the LICENCE TO
        NARROW, and the refusal caps the verdict exactly as §3.3 caps on an
        unsettled obligation.

        Two kinds of statement the harness will not narrow on:

        * a NON-TERMINAL dispatch (`dispatch_terminated` False — a detached job
          that vanished with a synthesized exit code, one a timeout monitor
          killed). Its module list is a PREFIX: Gradle prints
          `> Task :m:compileJava` incrementally, so a 300-module build OOM-killed
          at module 40 names exactly 40, and narrowing to those 40 would read the
          260 the build never reached as "untried, not unbuilt". Round three drew
          the right conclusion (do not narrow) from the wrong mechanism (drop the
          receipt), and dropping it let a scoped `-pl m0` retry narrow 26 modules
          to one.
        * a receipt LINE THAT DID NOT PARSE, or a probe that threw. It states
          nothing we may act on AND hides whatever it did state, so the honest
          answer is the wide denominator plus a cap. Swallowing it into "nothing
          stated" is P4's third verb, and it is the one that lets a wide receipt
          disappear while a narrow one decides.

        `narrowing_licensed` with an empty `modules` is the honest default of a
        single-module build (Maven prints no reactor summary for one module) and
        of a receipt-free run: nothing to narrow on, and nothing to cap.
        """
        receipt_records = self._read_live_invocation_receipts()
        if receipt_records is None:
            return _AttemptedModules((), False, "build_receipts_unreadable")
        if not receipt_records:
            return _AttemptedModules((), True, None)
        from sag.agent.attempt_policy import (
            current_run_production_build_receipt,
            resolve_current_build_receipt_scope,
        )
        from sag.agent.invocation_receipts import active_receipt_run_id

        current_run_id = self.receipt_run_id or active_receipt_run_id()
        project_scope = project_dir or self.project_path
        scope = resolve_current_build_receipt_scope(
            self.docker_orchestrator,
            run_id=current_run_id,
            workspace_root=self.project_path,
            project_root=project_scope,
        )
        if not scope.available:
            logger.debug(f"attempted-module receipt scope unavailable: {scope.status}")
            return _AttemptedModules((), False, "build_receipt_scope_unavailable")
        modules: List[str] = []
        unproven = False
        for payload in receipt_records:
            receipt_id = str(payload.get("receipt_id") or "").strip()
            if not current_run_production_build_receipt(
                payload,
                receipt_id=receipt_id,
                run_id=current_run_id,
                target_sha=scope.target_sha or "",
                project_root=scope.project_root or "",
            ):
                # A durable receipt from another run, checkout, or domain is
                # historical evidence.  It cannot narrow THIS run's coverage.
                continue
            stated = False
            for entry in payload.get("module_outcomes") or ():
                name = str((entry or {}).get("module") or "").strip()
                if not name:
                    continue
                stated = True
                if name not in modules:
                    modules.append(name)
            # A non-terminal receipt that stated NO modules said nothing about
            # the structure, so there is nothing to refuse and nothing to cap;
            # its own unsettled-obligation cap is §3.3's job, not the
            # denominator's.
            if stated and not _dispatch_terminated(payload):
                unproven = True
        cap = "build_receipt_not_terminal" if unproven else None
        return _AttemptedModules(tuple(modules), cap is None, cap)

    def _receipt_structure(self) -> Dict[str, Any]:
        """The receipt-proven module structure, or {} (Plan 8 §3.6).

        A survey guess (`root_shape: single_module`, `build_islands: []`) is a
        proposal; a terminal receipt that named its modules is a statement.
        This reads the statement, so the denominator can stand on it even in a
        phase whose own dispatches stated nothing.
        """
        if not self.docker_orchestrator:
            self._receipt_structure_conflict = "build_requirements_unavailable"
            return {}
        try:
            from sag.agent.receipt_structure import read_module_structure
            from sag.tools.internal.build_preflight import read_live_build_requirements

            manifest_read = read_live_build_requirements(self.docker_orchestrator)
            if (
                not manifest_read.complete
                or manifest_read.conflict is not None
                or manifest_read.payload is None
            ):
                self._receipt_structure_conflict = "build_requirements_unavailable"
                return {}
            self._receipt_structure_conflict = None
            return read_module_structure(dict(manifest_read.payload)) or {}
        except Exception as exc:
            logger.debug(f"receipt-proven structure unavailable: {exc}")
            self._receipt_structure_conflict = "build_requirements_unavailable"
            return {}

    def _module_scan_result(self, project_name: str) -> Optional[Dict[str, Any]]:
        """One walk of the tree: which modules exist, which produced output."""
        try:
            from sag.agent.module_coverage import module_coverage

            return module_coverage(self, project_name)
        except Exception as exc:
            logger.debug(f"module scan unavailable: {exc}")
            return None

    def module_scan(self, project_name: str) -> Optional[Dict[str, Any]]:
        """The scan THIS gate pass decided on (Plan 8 §3.5 / P3).

        `validate_build_status` scans and stores; the checklist line the gate
        appends reads the stored object. Before p7d these were two walks, and
        the two halves of one sentence disagreed: "Built 100% of expected
        classes · Module coverage: 1/26 built". Two computations answering one
        question is how the wrong one ends up deciding.
        """
        cached = getattr(self, "_last_module_scan", None)
        if cached and cached[0] == project_name:
            return cached[1]
        scan = self._module_scan_result(project_name)
        self._last_module_scan = (project_name, scan)
        return scan

    @staticmethod
    def _module_key(value: str) -> str:
        """Comparable form of a module label: lowercase alphanumerics only.

        Maven prints `<name>` ("Apache Camel :: Core"), the expectation path
        carries the directory ("core"). Equality on the normalized tail is the
        only match this claims; anything less certain is left unmatched, and
        an unmatched expectation keeps the denominator rather than quietly
        leaving it.

        The formula lives in `sag.agent.receipt_structure` because the
        persisted structure fact (Plan 8 §3.6) keys its module list the same
        way. Two copies of a key formula is two copies that drift.
        """
        return _receipt_module_key(value)

    def _scope_expectations_to_attempted(
        self,
        expected_artifacts: List[Dict[str, Any]],
        attempted: Optional[Tuple[str, ...]],
    ) -> "_ExpectationScope":
        """Narrow coverage to what ran, and state WHICH denominator came out.

        The narrowing happens ONLY when every attempted module maps to an
        expectation. A partial mapping would drop expectations for modules
        that did run and inflate the score, which is worse than the wide
        denominator it replaces; in that case the expectations stand and the
        disagreement is recorded instead.

        `denominator_modules` is the outcome's own answer to "which computation
        set the denominator": the attempted list when the narrowing took effect,
        `None` when the survey's wide expectation list stood. It is the single
        place that question is answered (spec §2 P3) — see the call site.
        """
        if not expected_artifacts or not attempted:
            return _ExpectationScope(list(expected_artifacts), [], None, None)
        attempted_keys = {self._module_key(name) for name in attempted} - {""}
        if not attempted_keys:
            return _ExpectationScope(list(expected_artifacts), [], None, None)
        scoped: List[Dict[str, Any]] = []
        untried: List[str] = []
        matched_keys = set()
        for expectation in expected_artifacts:
            key = self._module_key(str(expectation.get("path") or "").rsplit("/target", 1)[0])
            if key and key in attempted_keys:
                matched_keys.add(key)
                scoped.append(expectation)
            elif key:
                untried.append(key)
            else:
                scoped.append(expectation)
        if matched_keys != attempted_keys:
            # The build named modules this expectation list does not contain:
            # the mapping is incomplete, so the wide denominator stands and
            # says so rather than a narrowed one nobody can check. The receipt
            # did NOT set the denominator here, so it is not the authority
            # either — `denominator_modules` stays None and the scan cap is live.
            return _ExpectationScope(
                list(expected_artifacts),
                [],
                "build_coverage_scope_unverified",
                None,
            )
        return _ExpectationScope(scoped, untried, None, tuple(attempted))

    def _invocation_receipts_state(self) -> str:
        """Three answers from the strict current host-publication snapshot."""

        records = self._read_live_invocation_receipts()
        if records is None:
            return _RECEIPTS_UNREADABLE
        return _RECEIPTS_PRESENT if records else _RECEIPTS_ABSENT

    def _invocation_receipts_present(self) -> bool:
        """Presence, byte-identically to before: only a probe that SAW the
        directory is presence, and a probe that failed is not."""
        return self._invocation_receipts_state() == _RECEIPTS_PRESENT

    def _primary_test_coordinate_root(self) -> Optional[str]:
        """attempt_policy's primary test coordinate (Plan 4), or None.

        Receipts alone cannot say which invocation is *primary*; that is the
        survey coordinate's job. There is no legacy-scan fallback behind this
        any more (universal claim scoping, 2026-08-14). ``None`` means only that
        `_verified_report_claims` cannot narrow the ledger to one coordinate, so
        it admits every run-wide receipt's claims; the claim partition itself
        still runs, and the caller names the missing coordinate out loud
        (`test_primary_coordinate_unresolved`) rather than pretending it scoped.
        The resolution is cached on ``_last_test_candidate_resolution`` for the
        callers that need to say WHY the coordinate is absent.
        """
        try:
            from sag.agent import attempt_policy

            resolution = attempt_policy.resolve_survey_test_candidates(self.docker_orchestrator)
            self._last_test_candidate_resolution = resolution
        except Exception as exc:
            self._last_test_candidate_resolution = None
            logger.debug(f"Primary test coordinate unresolved: {exc}")
            return None
        primary = getattr(resolution, "primary", None)
        root = getattr(primary, "root", None)
        return root or None

    def _receipt_evidence_failure(
        self,
        test_result: Dict[str, any],
        cache_key: str,
        message: str,
        error_files: List[str],
    ) -> Dict[str, any]:
        """Fail closed: unreadable receipts are an evidence-closure failure.

        Persistence/readback failure is never "no evidence" — it means the
        provenance of every scanned report is unknown, so no count may be
        published and no phase may close on it.
        """
        logger.error(f"❌ Receipt-scoped test evidence unavailable: {message}")
        test_result["valid"] = False
        test_result["receipt_error"] = message
        if error_files:
            test_result["receipt_error_files"] = list(error_files)
        test_result["metrics_conflicts"] = sorted(
            {*(test_result.get("metrics_conflicts") or ()), "test_receipt_unreadable"}
        )
        test_result["parsing_errors"] = [*(test_result.get("parsing_errors") or []), message]
        self._cache_result(cache_key, test_result)
        return test_result

    def _parse_test_reports_compact_in_container(
        self,
        project_dir: str,
        primary_root: Optional[str] = None,
        receipt_records: Optional[List[Mapping[str, Any]]] = None,
    ) -> Optional[Dict[str, any]]:
        """Parse Maven/Gradle test XML inside the container and return compact JSON.

        The normal orchestrator output path intentionally truncates large text
        outputs. That is appropriate for logs, but corrupts XML facts when a
        validator does ``find`` followed by ``cat``. This parser keeps all XML
        reading local to the container and only returns aggregate metrics.

        ``primary_root`` is attempt_policy's primary test coordinate; with it
        the claim set is narrowed to that coordinate's receipts. ``None`` (no
        resolvable coordinate) narrows nothing — every claim of every current
        receipt counts — but it never widens the rollup past the claims.

        Returns ``None`` only when the parser did not run or produced no
        readable JSON — a run that found nothing still returns its dict, so
        callers can tell "no reports" apart from "no parser".
        """
        from sag.tools.internal.python_tool import PYTEST_REPORT_DIR

        records = (
            self._read_live_invocation_receipts()
            if receipt_records is None
            else [dict(record) for record in receipt_records]
        )
        if records is None:
            return {
                "valid": False,
                "receipt_error": "invocation receipt ledger is not host-authorized and complete",
                "receipt_error_files": [],
            }
        assessments = self._read_live_evidence_assessments()
        if assessments is None:
            return {
                "valid": False,
                "receipt_error": "evidence assessment ledger is not host-authorized and complete",
                "receipt_error_files": [],
            }
        receipt_claims = self._verified_report_claims(records, primary_root)
        parser_input = json.dumps(
            {
                "project_dir": project_dir,
                "pytest_reports_dir": PYTEST_REPORT_DIR,
                "receipt_claims": receipt_claims,
                "primary_root": primary_root,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        parser_input_sha256 = hashlib.sha256(parser_input.encode("utf-8")).hexdigest()
        workspace_root = posixpath.dirname(posixpath.normpath(project_dir))
        parser_input_path = posixpath.join(
            workspace_root,
            ".setup_agent",
            "validator_inputs",
            f"test-report-parser-{parser_input_sha256[:20]}.json",
        )
        persisted = write_container_text_atomic(
            self.docker_orchestrator,
            parser_input_path,
            parser_input,
            validate_json=True,
        )
        if not persisted.persisted:
            return {
                "valid": False,
                "receipt_error": (
                    "receipt claim transport could not persist the bounded parser input "
                    f"({persisted.code})"
                ),
                "receipt_error_files": [],
            }
        command = (
            "python3 - <<'PY'\n"
            "# SAG_COMPACT_TEST_REPORT_PARSER\n"
            "import json\n"
            f"with open({json.dumps(parser_input_path)}, encoding='utf-8') as source:\n"
            "    parser_input = json.load(source)\n"
            "project_dir = parser_input['project_dir']\n"
            "pytest_reports_dir = parser_input['pytest_reports_dir']\n"
            "receipt_claims = parser_input['receipt_claims']\n"
            "primary_root = parser_input['primary_root']\n"
            f"{_COMPACT_REPORT_PARSER_BODY}\n"
            "PY"
        )
        result = self.docker_orchestrator.execute_command(command)
        if result.get("exit_code") != 0 or not result.get("output"):
            return None

        output = result.get("output", "").strip()
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            start = output.find("{")
            end = output.rfind("}")
            if start < 0 or end <= start:
                logger.debug("Compact test report parser returned non-JSON output")
                return None
            try:
                parsed = json.loads(output[start : end + 1])
            except json.JSONDecodeError:
                logger.debug("Compact test report parser JSON was not parseable")
                return None

        if not isinstance(parsed, dict):
            return None
        if parsed.get("receipt_error"):
            logger.error(f"📊 Invocation-receipt evidence is unreadable: {parsed['receipt_error']}")
            return parsed
        if not parsed.get("valid"):
            return parsed

        logger.info(
            "📊 Compact test report analysis: "
            f"{parsed.get('total_tests', 0)} total, "
            f"{parsed.get('passed_tests', 0)} passed, "
            f"{parsed.get('failed_tests', 0)} failed, "
            f"{parsed.get('error_tests', 0)} errors, "
            f"{parsed.get('skipped_tests', 0)} skipped "
            f"from {parsed.get('report_file_count', len(parsed.get('report_files') or []))} XML files"
        )
        return parsed

    def _parse_single_test_xml(self, xml_content: str, file_path: str) -> Optional[Dict[str, int]]:
        """
        Parse a single test XML file and extract statistics.
        Handles both Maven Surefire and Gradle formats.
        """
        try:
            root = ET.fromstring(xml_content)
            testcase_entries: List[Dict[str, str]] = []

            # Maven Surefire format: <testsuite tests="X" failures="Y" errors="Z" skipped="W">
            if root.tag == "testsuite":
                # Collect all testcases first
                all_testcases = self._collect_testcases_from_suite(root, file_path)

                # Collection nodes never executed -> they leave the runtime set
                # entirely (Plan 4 Task 2).
                testcase_entries, collection = _partition_collection_nodes(all_testcases)

                # Recalculate statistics over every runtime testcase (spec §3.4-4)
                total = len(testcase_entries)
                failures = sum(1 for tc in testcase_entries if tc.get("status") == "failed")
                errors = sum(1 for tc in testcase_entries if tc.get("status") == "error")
                skipped = sum(1 for tc in testcase_entries if tc.get("status") == "skipped")
                passed = sum(1 for tc in testcase_entries if tc.get("status") == "passed")

                return {
                    "total": total,
                    "passed": max(0, passed),  # Ensure non-negative
                    "failed": failures,
                    "errors": errors,
                    "skipped": skipped,
                    "testcases": testcase_entries,
                    **collection,
                }

            # Gradle format: <testsuites> containing multiple <testsuite>
            elif root.tag == "testsuites":
                # Collect all testcases from all suites
                all_testcases = []
                for testsuite in root.findall("testsuite"):
                    all_testcases.extend(self._collect_testcases_from_suite(testsuite, file_path))

                testcase_entries, collection = _partition_collection_nodes(all_testcases)

                # Calculate statistics over every runtime testcase (spec §3.4-4)
                total = len(testcase_entries)
                failures = sum(1 for tc in testcase_entries if tc.get("status") == "failed")
                errors = sum(1 for tc in testcase_entries if tc.get("status") == "error")
                skipped = sum(1 for tc in testcase_entries if tc.get("status") == "skipped")
                passed = sum(1 for tc in testcase_entries if tc.get("status") == "passed")

                return {
                    "total": total,
                    "passed": max(0, passed),
                    "failed": failures,
                    "errors": errors,
                    "skipped": skipped,
                    "testcases": testcase_entries,
                    **collection,
                }

            # Try to find testsuite elements even if root is different
            testsuites = root.findall(".//testsuite")
            if testsuites:
                # Collect all testcases from all suites
                all_testcases = []
                for testsuite in testsuites:
                    all_testcases.extend(self._collect_testcases_from_suite(testsuite, file_path))

                testcase_entries, collection = _partition_collection_nodes(all_testcases)

                # An all-collection-nodes file still parsed successfully: it
                # reports zero executed tests, not a parse failure.
                saw_collection_nodes = (
                    collection["collection_errors"] or collection["collection_errors_skipped"]
                )
                if testcase_entries or saw_collection_nodes:
                    # Calculate statistics over every runtime testcase (spec §3.4-4)
                    total = len(testcase_entries)
                    failures = sum(1 for tc in testcase_entries if tc.get("status") == "failed")
                    errors = sum(1 for tc in testcase_entries if tc.get("status") == "error")
                    skipped = sum(1 for tc in testcase_entries if tc.get("status") == "skipped")
                    passed = sum(1 for tc in testcase_entries if tc.get("status") == "passed")

                    return {
                        "total": total,
                        "passed": max(0, passed),
                        "failed": failures,
                        "errors": errors,
                        "skipped": skipped,
                        "testcases": testcase_entries,
                        **collection,
                    }
                return None

            logger.warning(f"Unrecognized XML format in {file_path}, root tag: {root.tag}")
            return None

        except ET.ParseError as e:
            logger.warning(f"XML parsing error in {file_path}: {e}")
            # Try fallback extraction instead of returning None
            return self._extract_test_stats_fallback(xml_content, file_path)
        except (ValueError, AttributeError) as e:
            logger.warning(f"Data extraction error in {file_path}: {e}")
            # Try fallback extraction for other errors too
            return self._extract_test_stats_fallback(xml_content, file_path)
        except Exception as e:
            logger.warning(f"Unexpected error parsing {file_path}: {e}")
            # Always try fallback rather than losing data
            return self._extract_test_stats_fallback(xml_content, file_path)

    def _check_modules_without_tests(self, project_dir: str, report_dirs: List[str]) -> List[str]:
        """
        Check if this is a multi-module project and identify modules without test reports.

        Returns:
            List of module names that lack test reports
        """
        modules_without_tests = []

        try:
            # Check if root pom.xml has <modules> section
            pom_check_cmd = f"test -f {project_dir}/pom.xml && echo 'EXISTS' || echo 'MISSING'"
            pom_result = self._execute_command_with_logging(
                pom_check_cmd, "checking for root pom.xml"
            )

            if not pom_result["success"] or "MISSING" in pom_result.get("output", ""):
                return []  # Not a Maven project or no root POM

            # Extract modules from pom.xml
            modules_cmd = f"grep -A 100 '<modules>' {project_dir}/pom.xml 2>/dev/null | grep -B 100 '</modules>' | grep '<module>' | sed 's/<module>//g' | sed 's/<\\/module>//g' | tr -d ' \\t'"
            modules_result = self._execute_command_with_logging(
                modules_cmd, "extracting Maven modules"
            )

            if not modules_result["success"] or not modules_result.get("output"):
                return []  # No modules found

            modules = [m.strip() for m in modules_result["output"].split("\n") if m.strip()]

            if not modules:
                return []  # Not a multi-module project

            logger.debug(f"Found {len(modules)} modules in project: {modules}")

            # Convert report_dirs to set for faster lookup
            report_dir_set = set(report_dirs)

            # Check each module for test reports
            for module in modules:
                module_dir = f"{project_dir}/{module}"

                # Check if module has any test reports
                has_reports = False
                for potential_report_dir in [
                    f"{module_dir}/target/surefire-reports",
                    f"{module_dir}/target/failsafe-reports",
                    f"{module_dir}/build/test-results",
                ]:
                    if potential_report_dir in report_dir_set:
                        has_reports = True
                        break

                if not has_reports:
                    # Double-check by looking for any XML test reports
                    check_cmd = f"find {module_dir} -path '*/target/surefire-reports/*.xml' -o -path '*/target/failsafe-reports/*.xml' 2>/dev/null | head -1"
                    check_result = self._execute_command_with_logging(
                        check_cmd, f"checking for test reports in {module}"
                    )

                    if not check_result["success"] or not check_result.get("output", "").strip():
                        modules_without_tests.append(module)
                        logger.debug(f"Module {module} has no test reports")

            return modules_without_tests

        except Exception as e:
            logger.warning(f"Failed to check modules without tests: {e}")
            return []

    def _collect_testcases_from_suite(
        self, testsuite: ET.Element, file_path: str
    ) -> List[Dict[str, any]]:
        """Collect individual testcase entries from a testsuite element."""
        cases: List[Dict[str, any]] = []
        for testcase in testsuite.findall("testcase"):
            name = (testcase.get("name") or "").strip()
            classname = (testcase.get("classname") or "").strip()
            identity_file = testcase.get("file") or testsuite.get("file")
            file_attr = identity_file or file_path
            time_attr = testcase.get("time")
            status = self._determine_testcase_status(testcase)
            collection_kind = _collection_node_kind(testcase)
            entry = {
                "name": name,
                "classname": classname,
                "file": file_attr,
                "identity_file": identity_file,
                "status": status,
                "time": float(time_attr) if time_attr else 0.0,
                "collection_node": collection_kind,
            }
            if collection_kind == "error":
                error = testcase.find("error")
                entry["collection_message"] = _structured_error_line(
                    error.text if error is not None else "",
                    (error.get("message") if error is not None else "") or "",
                )
            cases.append(entry)
        # Debug logging
        if len(cases) > 0:
            logger.debug(f"Collected {len(cases)} testcases from {file_path}")
        return cases

    @staticmethod
    def _determine_testcase_status(testcase: ET.Element) -> str:
        """Determine the status of a testcase element."""
        # JUnit 5 marks status via child elements
        if testcase.find("error") is not None:
            return "error"
        if testcase.find("failure") is not None:
            return "failed"
        if testcase.find("skipped") is not None or testcase.get("status") == "skipped":
            return "skipped"
        return "passed"

    def _save_unexecuted_tests_log(
        self,
        project_dir: str,
        unexecuted_tests: List[Dict],
        test_catalog: Optional[TestCaseCatalog] = None,
    ):
        """
        Save unexecuted tests to a JSON log file for post-analysis review.

        The log file is saved to /workspace/.setup_agent/unexecuted_tests.json.
        It includes metadata about when the analysis was performed and what tests were expected.

        Args:
            project_dir: Project directory path (used for metadata, not for save location)
            unexecuted_tests: List of unexecuted test dictionaries
            test_catalog: Optional test catalog for additional metadata
        """
        try:
            # Create .setup_agent directory in /workspace if it doesn't exist
            setup_agent_dir = "/workspace/.setup_agent"
            if self.docker_orchestrator:
                mkdir_cmd = f"mkdir -p {setup_agent_dir}"
                self.docker_orchestrator.execute_command(mkdir_cmd)

            # Prepare log data
            log_data = {
                "timestamp": datetime.now().isoformat(),
                "project_dir": project_dir,
                "total_unexecuted": len(unexecuted_tests),
                "total_expected": test_catalog.count() if test_catalog else None,
                "unexecuted_tests": unexecuted_tests,
                "summary": {"by_module": {}, "by_package": {}, "by_class": {}},
            }

            # Generate summaries
            for test in unexecuted_tests:
                # By module
                module = test.get("module", "default")
                if module not in log_data["summary"]["by_module"]:
                    log_data["summary"]["by_module"][module] = []
                log_data["summary"]["by_module"][module].append(test["key"])

                # By package
                package = test.get("package", "unknown")
                if package not in log_data["summary"]["by_package"]:
                    log_data["summary"]["by_package"][package] = []
                log_data["summary"]["by_package"][package].append(test["key"])

                # By class
                class_name = f"{test.get('package', '')}.{test.get('class', '')}"
                if class_name not in log_data["summary"]["by_class"]:
                    log_data["summary"]["by_class"][class_name] = []
                log_data["summary"]["by_class"][class_name].append(test["method"])

            # Count summaries
            log_data["summary"]["module_counts"] = {
                module: len(tests) for module, tests in log_data["summary"]["by_module"].items()
            }
            log_data["summary"]["package_counts"] = {
                pkg: len(tests) for pkg, tests in log_data["summary"]["by_package"].items()
            }
            log_data["summary"]["class_counts"] = {
                cls: len(methods) for cls, methods in log_data["summary"]["by_class"].items()
            }

            # Save to JSON file
            log_file_path = os.path.join(setup_agent_dir, "unexecuted_tests.json")
            json_content = json.dumps(log_data, indent=2, ensure_ascii=False)

            if self.docker_orchestrator:
                # Write file using chunked approach for large data
                # This is the most robust solution for any data size

                # For small data (< 100KB), use simple heredoc
                if len(json_content) < 100000:
                    write_cmd = f"cat > {log_file_path} << 'EOF'\n{json_content}\nEOF"
                    result = self.docker_orchestrator.execute_command(write_cmd)
                else:
                    # For large data, write in chunks to avoid "argument list too long"
                    temp_file = "/tmp/unexecuted_tests_temp.json"

                    # First, ensure temp file doesn't exist
                    self.docker_orchestrator.execute_command(f"rm -f {temp_file}")

                    # Write to temp file in chunks
                    chunk_size = 50000  # 50KB chunks - very safe size
                    result = {"exit_code": 0}

                    for i in range(0, len(json_content), chunk_size):
                        chunk = json_content[i : i + chunk_size]
                        # Use base64 for each chunk to avoid escaping issues
                        import base64

                        encoded_chunk = base64.b64encode(chunk.encode("utf-8")).decode("ascii")

                        # Append each chunk (first chunk creates file)
                        append_symbol = ">>" if i > 0 else ">"
                        write_chunk_cmd = (
                            f"echo '{encoded_chunk}' | base64 -d {append_symbol} {temp_file}"
                        )

                        chunk_result = self.docker_orchestrator.execute_command(write_chunk_cmd)
                        if chunk_result.get("exit_code") != 0:
                            result = chunk_result
                            logger.warning(f"Failed to write chunk {i//chunk_size + 1}")
                            break

                    # If all chunks written successfully, move to final location
                    if result.get("exit_code") == 0:
                        move_cmd = f"mv {temp_file} {log_file_path}"
                        result = self.docker_orchestrator.execute_command(move_cmd)

                # Check exit_code
                if result.get("exit_code") == 0:
                    logger.info(f"📝 Saved unexecuted tests log to: {log_file_path}")
                    logger.info(f"   Review with: cat {log_file_path}")
                else:
                    logger.warning(
                        f"Failed to save unexecuted tests log: exit_code={result.get('exit_code')}, error={result.get('error', 'Unknown error')}"
                    )
            else:
                # Fallback: write directly if no orchestrator (for testing)
                with open(log_file_path, "w", encoding="utf-8") as f:
                    json.dump(log_data, f, indent=2, ensure_ascii=False)
                logger.info(f"📝 Saved unexecuted tests log to: {log_file_path}")

            # Also create a simple text version for quick review
            self._save_unexecuted_tests_text_log(setup_agent_dir, log_data)

        except Exception as e:
            logger.error(f"Failed to save unexecuted tests log: {e}")

    def _save_unexecuted_tests_text_log(self, setup_agent_dir: str, log_data: Dict):
        """
        Save a human-readable text version of unexecuted tests.

        Args:
            setup_agent_dir: Directory to save the log file
            log_data: Structured log data from JSON
        """
        try:
            text_content = []
            text_content.append("=" * 80)
            text_content.append("UNEXECUTED TESTS REPORT")
            text_content.append("=" * 80)
            text_content.append(f"Generated: {log_data['timestamp']}")
            text_content.append(f"Project: {log_data['project_dir']}")
            text_content.append(f"Total Unexecuted: {log_data['total_unexecuted']}")
            if log_data.get("total_expected"):
                text_content.append(f"Total Expected: {log_data['total_expected']}")
                coverage = (
                    (log_data["total_expected"] - log_data["total_unexecuted"])
                    / log_data["total_expected"]
                    * 100
                )
                text_content.append(f"Test Coverage: {coverage:.1f}%")
            text_content.append("")

            # Summary by module
            if log_data["summary"]["module_counts"]:
                text_content.append("BY MODULE:")
                for module, count in sorted(log_data["summary"]["module_counts"].items()):
                    text_content.append(f"  {module}: {count} unexecuted tests")
                text_content.append("")

            # Summary by package
            if log_data["summary"]["package_counts"]:
                text_content.append("BY PACKAGE:")
                for package, count in sorted(log_data["summary"]["package_counts"].items()):
                    text_content.append(f"  {package}: {count} unexecuted tests")
                text_content.append("")

            # Detailed list
            text_content.append("DETAILED LIST OF UNEXECUTED TESTS:")
            text_content.append("-" * 80)
            for i, test in enumerate(log_data["unexecuted_tests"], 1):
                text_content.append(f"{i}. {test['key']}")
                text_content.append(f"   Module: {test.get('module', 'N/A')}")
                text_content.append(f"   File: {test.get('path', 'N/A')}")
                text_content.append("")

            # Join all lines
            text_report = "\n".join(text_content)

            # Save text file
            text_file_path = os.path.join(setup_agent_dir, "unexecuted_tests.txt")
            if self.docker_orchestrator:
                # Write text file using same chunked approach as JSON

                # For small text (< 100KB), use simple heredoc
                if len(text_report) < 100000:
                    write_cmd = f"cat > {text_file_path} << 'EOF'\n{text_report}\nEOF"
                    result = self.docker_orchestrator.execute_command(write_cmd)
                else:
                    # For large text, write in chunks to avoid "argument list too long"
                    temp_file = "/tmp/unexecuted_tests_temp.txt"

                    # Clean up any existing temp file
                    self.docker_orchestrator.execute_command(f"rm -f {temp_file}")

                    # Write to temp file in chunks
                    chunk_size = 50000  # 50KB chunks - safe for all systems
                    result = {"exit_code": 0}

                    for i in range(0, len(text_report), chunk_size):
                        chunk = text_report[i : i + chunk_size]
                        # Use base64 for each chunk to handle special characters
                        import base64

                        encoded_chunk = base64.b64encode(chunk.encode("utf-8")).decode("ascii")

                        # Append each chunk (first chunk creates file)
                        append_symbol = ">>" if i > 0 else ">"
                        write_chunk_cmd = (
                            f"echo '{encoded_chunk}' | base64 -d {append_symbol} {temp_file}"
                        )

                        chunk_result = self.docker_orchestrator.execute_command(write_chunk_cmd)
                        if chunk_result.get("exit_code") != 0:
                            result = chunk_result
                            logger.warning(f"Failed to write text chunk {i//chunk_size + 1}")
                            break

                    # If all chunks written successfully, move to final location
                    if result.get("exit_code") == 0:
                        move_cmd = f"mv {temp_file} {text_file_path}"
                        result = self.docker_orchestrator.execute_command(move_cmd)

                # Check exit_code
                if result.get("exit_code") == 0:
                    logger.info(f"📄 Saved text report to: {text_file_path}")
                else:
                    logger.warning(
                        f"Failed to save text report: exit_code={result.get('exit_code')}, error={result.get('error', 'Unknown error')}"
                    )
            else:
                with open(text_file_path, "w", encoding="utf-8") as f:
                    f.write(text_report)

        except Exception as e:
            logger.error(f"Failed to save text log: {e}")

    def _extract_test_stats_fallback(self, xml_content: str, file_path: str) -> Dict[str, int]:
        """
        Fallback regex extraction when XML parsing fails.
        This ensures we don't lose test counts from malformed XML files.

        Args:
            xml_content: Raw XML content
            file_path: Path to the XML file for logging

        Returns:
            Dictionary with test statistics (never None)
        """
        try:
            # Try to extract from testsuite tag attributes using regex
            # Handle both single-line and multi-line testsuite tags
            import re

            # Pattern to match testsuite opening tag and extract attributes
            # This pattern is flexible with attribute order
            testsuite_pattern = r"<testsuite[^>]*?>"
            match = re.search(testsuite_pattern, xml_content, re.IGNORECASE)

            testcase_entries = []

            # Try to extract individual testcases with regex for deduplication
            # Extract all testcase tags first
            testcase_tags = re.findall(
                r"<testcase\s+[^>]*?/?>", xml_content, re.IGNORECASE | re.DOTALL
            )

            for testcase_tag in testcase_tags:
                # Extract name and classname from each tag (order-independent)
                # Use word boundary \b to ensure we match the attribute name correctly
                name_match = re.search(r'\bname=["\']([^"\']*)["\']', testcase_tag)
                classname_match = re.search(r'\bclassname=["\']([^"\']*)["\']', testcase_tag)

                # Both name and classname are important for deduplication
                if name_match and classname_match:
                    name = name_match.group(1)
                    classname = classname_match.group(1)

                    testcase_entries.append(
                        {
                            "name": name,
                            "classname": classname,
                            "file": file_path,
                            "status": "passed",  # Default to passed in fallback
                        }
                    )
                elif name_match:
                    # If only name is found, still add it
                    testcase_entries.append(
                        {
                            "name": name_match.group(1),
                            "classname": "",
                            "file": file_path,
                            "status": "passed",
                        }
                    )

            if match:
                testsuite_tag = match.group(0)

                # Extract individual attributes
                tests_match = re.search(r'tests=["\'](\d+)["\']', testsuite_tag)
                failures_match = re.search(r'failures=["\'](\d+)["\']', testsuite_tag)
                errors_match = re.search(r'errors=["\'](\d+)["\']', testsuite_tag)
                skipped_match = re.search(r'skipped=["\'](\d+)["\']', testsuite_tag)

                if tests_match:
                    total = int(tests_match.group(1))
                    failures = int(failures_match.group(1)) if failures_match else 0
                    errors = int(errors_match.group(1)) if errors_match else 0
                    skipped = int(skipped_match.group(1)) if skipped_match else 0
                    passed = total - failures - errors - skipped

                    logger.info(
                        f"Recovered stats from malformed XML {file_path}: {total} tests, {len(testcase_entries)} testcases"
                    )
                    return {
                        "total": total,
                        "passed": max(0, passed),
                        "failed": failures,
                        "errors": errors,
                        "skipped": skipped,
                        "testcases": testcase_entries,
                    }

            # If we can't extract from testsuite, try counting testcase tags as last resort
            testcase_count = len(testcase_entries)
            if testcase_count > 0:
                logger.info(f"Counted {testcase_count} testcase tags in {file_path}")
                # We can't determine pass/fail from just counting tags
                return {
                    "total": testcase_count,
                    "passed": testcase_count,  # Assume passed unless we know otherwise
                    "failed": 0,
                    "errors": 0,
                    "skipped": 0,
                    "testcases": testcase_entries,
                }

        except Exception as e:
            logger.warning(f"Fallback extraction also failed for {file_path}: {e}")

        # Return zeros instead of None to prevent losing the file entirely
        # This way we at least know a file existed even if we couldn't parse it
        logger.warning(f"Could not extract any test data from {file_path}, returning zeros")
        return {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "testcases": []}

    def validate_build_status(self, project_name: str) -> Dict[str, any]:
        """
        Build status validation based on physical evidence hierarchy.
        Does NOT execute build commands to avoid environment dependencies.
        ONLY validates build/compilation status, NOT test results.

        Args:
            project_name: Name of the project

        Returns:
            Dict with:
                - success: bool (determined by build evidence hierarchy)
                - evidence: dict of build validation details
                - reason: str (explanation of the decision)
        """
        logger.info(f"Starting build artifact validation for project: {project_name}")

        project_dir = f"{self.project_path}/{project_name}" if project_name else self.project_path

        # Collect build evidence only (no test-related checks).
        # tool / module_output_count / artifact_samples / warnings are surfaced
        # here because report_metrics.assemble_report_metrics reads them straight
        # off this evidence dict; leaving them unset makes those UI fields dead.
        evidence = {
            "build_system": None,
            "tool": None,
            "has_artifacts": False,
            "artifact_count": 0,
            "has_build_fingerprints": False,
            "fingerprint_details": {},
            "module_output_count": None,
            "artifact_samples": [],
            "warnings": [],
        }

        # Detect build system
        build_system = self._detect_build_system(project_dir)
        evidence["build_system"] = build_system
        # "tool" mirrors the detected build system. The metrics layer reads
        # build_evidence["tool"]; without this it always name-drifted to None.
        evidence["tool"] = build_system if build_system != "unknown" else None
        logger.info(f"Detected build system: {build_system}")
        terminal_reactor = (
            self._terminal_root_maven_reactor_receipt(project_dir)
            if build_system == "maven"
            else None
        )
        if terminal_reactor is not None:
            evidence.update(
                {
                    "authority": "terminal_reactor_receipt",
                    "terminal_reactor_receipt_id": terminal_reactor["receipt_id"],
                    "reactor_modules_succeeded": terminal_reactor["modules_succeeded"],
                    "reactor_modules_total": terminal_reactor["modules_total"],
                }
            )

        # Check 1: Build artifacts
        artifacts_result = self._check_build_artifacts_complete(project_dir)
        evidence["has_artifacts"] = artifacts_result["exist"]
        evidence["artifact_count"] = artifacts_result["count"]
        # Keep the validator-owned physical count in the structured evidence.
        # Phase gates persist this scalar into RunEvidenceState before
        # evidence-close; without it the canonical verdict snapshot can prove a
        # build happened but cannot reproduce reactor benchmark class counts.
        evidence["class_count"] = artifacts_result["class_count"]
        evidence["jar_count"] = artifacts_result["jar_count"]
        evidence["artifact_samples"] = self._collect_artifact_samples(project_dir, artifacts_result)
        if evidence["has_artifacts"]:
            logger.info(
                f"✅ Found {artifacts_result['count']} build artifacts (JARs: {artifacts_result['jar_count']}, Classes: {artifacts_result['class_count']})"
            )
        else:
            logger.info("❌ No build artifacts found")

        # Check 2: Build system fingerprints
        python_build = None
        if build_system == "maven":
            fingerprints = self._validate_maven_fingerprints(project_dir)
            evidence["has_build_fingerprints"] = fingerprints["valid"]
            evidence["fingerprint_details"] = fingerprints["details"]
            # Modules that actually produced build output (target/maven-status).
            evidence["module_output_count"] = len(fingerprints.get("modules") or [])
            if fingerprints["valid"]:
                logger.info(f"✅ Maven build fingerprints found: {fingerprints['details']}")
                if fingerprints["modules"]:
                    logger.info(f"   Multi-module project with modules: {fingerprints['modules']}")
        elif build_system == "gradle":
            cache = self._validate_gradle_cache(project_dir)
            evidence["has_build_fingerprints"] = cache["valid"]
            evidence["fingerprint_details"] = cache["details"]
            # Subprojects that actually produced build output (build/ dir).
            evidence["module_output_count"] = len(cache.get("subprojects") or [])
            if cache["valid"]:
                logger.info(f"✅ Gradle build cache found: {cache['details']}")
                if cache["subprojects"]:
                    logger.info(f"   Multi-project build with subprojects: {cache['subprojects']}")
        elif build_system == "python":
            # Python physical evidence is strictly observational.  Runtime
            # checks (pip/import/compile) belong to ordinary producer
            # invocations and their durable receipts; the judge never runs a
            # project interpreter or manufactures bytecode while deciding.
            python_build = self._verify_python_build(project_dir)
            evidence["has_build_fingerprints"] = python_build["venv_exists"]
            evidence["fingerprint_details"] = {
                key: python_build[key]
                for key in (
                    "venv_exists",
                    "pip_check_clean",
                    "distribution_record_ok",
                    "distribution_origin_ok",
                    "install_outcome",
                    "imports_ok",
                    "import_failures",
                    "compileall_coverage",
                    "compileall_metric_status",
                    "compileall_source_count",
                    "compileall_compiled_source_count",
                    "compileall_foreign_pyc_count",
                    "compileall_cache_tag",
                    "compileall_source_basis_sha256",
                    "compileall_pyc_basis_sha256",
                    "compileall_source_basis_entry_count",
                    "compileall_pyc_basis_entry_count",
                    "compileall_missing_sources",
                    "compileall_foreign_pycs",
                    "wheel_artifact_status",
                    "wheel_artifacts",
                    "wheel_artifacts_verified",
                    "producer_receipt_status",
                    "producer_receipt_ids",
                    "ext_modules_ok",
                    "native_artifact_ok",
                )
            }
            if isinstance(python_build.get("test_entry_ready"), bool):
                evidence["test_entry_ready"] = python_build["test_entry_ready"]
                if python_build.get("test_entry_candidate"):
                    evidence["test_entry_candidate"] = python_build["test_entry_candidate"]
            # Skipped-rung warnings (e.g. "imports rung skipped: ...") must be
            # VISIBLE in the report, not a silent None in the details.
            evidence["warnings"].extend(python_build.get("warnings") or [])
            logger.info(f"Python evidence ladder: {evidence['fingerprint_details']}")

        # Decision logic for BUILD ONLY (no test considerations).
        #
        # Tri-state policy (user mandate): a setup is a full SUCCESS only when
        # EVERY active module compiled. Real build output but incomplete module
        # coverage is PARTIAL (the build happened, but not all modules) — never a
        # clean success. No compiled evidence at all is BLOCKED.
        #
        # `success` means "the build is real / the build phase happened" (it stays
        # True for a partial build so the phase is not hard-failed); `complete`
        # means every expected/active module produced its output. The
        # build_modules_incomplete conflict (emitted when success but not complete)
        # is what caps the overall run verdict at partial downstream.
        success = False
        complete = False
        reason = ""

        # Per-module expectations are authoritative and must be checked even when
        # build fingerprints exist — fingerprints for SOME modules used to
        # short-circuit the whole build to success. Modules disabled in the build
        # config (commented out or in a profile proven inactive without -P) are
        # excluded; activeByDefault profile modules are included. A declared or
        # dynamically activated module that cannot be verified is omitted from
        # unsafe path probes but separately caps the verdict via the immutable
        # reactor snapshot below.
        expected_artifacts = (
            self._get_expected_artifacts(project_dir, build_system)
            if build_system in ("maven", "gradle")
            else []
        )
        maven_reactor_snapshot = (
            getattr(expected_artifacts, "reactor_snapshot", None)
            if build_system == "maven"
            else None
        )
        if isinstance(maven_reactor_snapshot, _MavenReactorSnapshot):
            evidence["maven_reactor_complete"] = maven_reactor_snapshot.complete
            evidence["maven_reactor_modules"] = [
                record.module_dir for record in maven_reactor_snapshot.records
            ]
            evidence["maven_reactor_conflicts"] = list(maven_reactor_snapshot.conflicts)
            if maven_reactor_snapshot.profile_selection.explicit:
                evidence["maven_profiles_enabled"] = sorted(
                    maven_reactor_snapshot.profile_selection.enabled
                )
                evidence["maven_profiles_disabled"] = sorted(
                    maven_reactor_snapshot.profile_selection.disabled
                )
            if maven_reactor_snapshot.profile_selection.conservative:
                evidence["maven_profile_selection_conservative"] = True
            if not maven_reactor_snapshot.complete:
                evidence["warnings"].append(
                    "Maven reactor could not be fully verified: " + maven_reactor_snapshot.reason
                )
        # Coverage is measured against what THIS run attempted, not against
        # every source tree on disk (Plan 7: the reactor summary sets the
        # scope, the file count verifies the substance).
        # Plan 8 §2 P4: the read states BOTH what the dispatches claimed and
        # whether the harness will narrow on that claim. A receipt it will not
        # narrow on still contributes its modules (they were attempted) and caps
        # the verdict; it never shrinks the denominator.
        attempted = self._attempted_module_evidence(project_dir)
        attempted_modules = attempted.modules or None
        # Plan 8 §3.6, and only as far as it may honestly go: the persisted
        # receipt-proven structure NAMES the receipt behind a denominator; it is
        # never a substitute for this pass's attempted-module list. Narrowing the
        # denominator is licensed only by what THIS pass's receipts say they
        # attempted — a structure proved by an earlier `mvn -pl core` would drop
        # every other module's expectation as "untried" and refine a partial
        # build UPWARD into a complete success, which is the exact P0-F
        # direction the plan forbids.
        receipt_structure = self._receipt_structure()
        scope = self._scope_expectations_to_attempted(
            list(expected_artifacts),
            attempted_modules if attempted.narrowing_licensed else None,
        )
        scoped_artifacts, untried_modules, scope_conflict = (
            scope.expectations,
            scope.untried,
            scope.conflict,
        )
        if attempted_modules:
            evidence["modules_attempted"] = list(attempted_modules)
        if untried_modules:
            evidence["modules_untried"] = untried_modules
        # Every reason the denominator was NOT narrowed to what ran, in one list.
        # Each is a cap below complete: the narrowing is licensed only by evidence
        # the harness will stand behind in both directions, and a refusal that
        # left no trace is how the p7d polaris sentence graded green.
        denominator_refusals: List[str] = []
        receipt_structure_conflict = getattr(self, "_receipt_structure_conflict", None)
        if receipt_structure_conflict:
            denominator_refusals.append(receipt_structure_conflict)
            evidence.setdefault("conflicts", []).append(receipt_structure_conflict)
            evidence["warnings"].append(
                "Host-published build requirements are unavailable; receipt-proven "
                "module structure cannot authorize a complete denominator"
            )
        if scope_conflict:
            denominator_refusals.append(scope_conflict)
            evidence.setdefault("conflicts", []).append(scope_conflict)
            evidence["warnings"].append(
                f"Build coverage is measured against {len(expected_artifacts)} module "
                f"expectation(s) while the build stated it attempted "
                f"{len(attempted_modules or ())}; the two could not be matched, so the "
                "wider denominator stands"
            )
        if attempted.cap:
            denominator_refusals.append(attempted.cap)
            evidence.setdefault("conflicts", []).append(attempted.cap)
            evidence["warnings"].append(
                "Build coverage was NOT narrowed to the modules this run attempted: "
                f"{_DENOMINATOR_REFUSALS.get(attempted.cap, attempted.cap)}; the wider "
                "denominator stands and the build cannot be graded complete"
            )
        coverage_info = (
            self._verify_expected_artifacts(project_dir, scoped_artifacts)
            if scoped_artifacts
            else None
        )
        # Rate-banded verdict v4 reuses this existing source-weighted census
        # beside the physical class count.  No second source-tree scan is
        # permitted at evidence close.
        evidence["source_files"] = (
            int(coverage_info.get("classes_expected") or 0)
            if isinstance(coverage_info, dict)
            and int(coverage_info.get("classes_expected") or 0) > 0
            else None
        )
        threshold = self.build_coverage_threshold
        class_count = artifacts_result.get("class_count", 0)
        has_real_output = evidence["has_build_fingerprints"] or class_count > 0
        # Plan 8 §3.5: ONE scan, two consumers. The walk happens here, the gate
        # renders its checklist from this same object (module_scan holds it),
        # and the denominator's authority is a stated ladder — a terminal
        # receipt, else this scan, else the survey's expectations.
        #
        # The rung comes from `scope.denominator_modules`, i.e. from the scoping
        # OUTCOME, not from "a receipt exists": when the narrowing was refused
        # (`build_coverage_scope_unverified` above) the receipt did not set this
        # pass's denominator — the survey's wide expectation list did — so the
        # receipt is not the authority and the scan cap below stays live. One
        # question, one computation (spec §2 P3).
        from sag.agent.module_coverage import module_basis

        self._last_module_scan = (project_name, self._module_scan_result(project_name))
        scan_result = self._last_module_scan[1]
        if isinstance(scan_result, dict) and scan_result.get("unreadable"):
            # §6.8 fence 3, the verdict half: a scan that could not read joins
            # the refusals, so the §3.5 cap fires — a run that could not check
            # its own denominator is uncertain, and uncertainty is never a
            # complete build. The checklist half states the same inability.
            denominator_refusals.append("module_scan_unreadable")
            evidence.setdefault("conflicts", []).append("module_scan_unreadable")
            evidence["warnings"].append(
                "The module scan could not read the tree; per-module coverage "
                "is unknown and the build cannot be graded complete"
            )
        basis = module_basis(
            self._last_module_scan[1],
            denominator_modules=scope.denominator_modules,
            structure=receipt_structure,
        )

        # Hard JVM gate (Part 1 principle, applied to EVERY branch): a maven/gradle
        # build is green only with compiled .class evidence. With zero compiled
        # classes AND no real build artifacts, it is BLOCKED — even if a coverage
        # default (no class-based expectation could be derived, so class_coverage
        # falls back to 1.0) or an empty target/classes fingerprint would otherwise
        # pass. Without this, commons-chain (0 classes, non-standard src layout) was
        # reported "Built 100% of expected classes" while the module scan showed
        # 0/1 built — the build verdict and the module report contradicting.
        jvm_no_compiled_evidence = (
            build_system in ("maven", "gradle")
            and class_count == 0
            and not evidence["has_artifacts"]
        )

        if build_system == "python" and python_build is not None:
            # A missing venv or exact installed-distribution ownership can be
            # rejected from filesystem facts. Runtime rungs without producer
            # receipts remain unknown and therefore cap the build at PARTIAL.
            success = python_build["success"]
            complete = python_build["complete"]
            reason = python_build["reason"]

        elif jvm_no_compiled_evidence:
            success, complete = False, False
            reason = (
                f"No compiled .class files found for {build_system} build — nothing "
                f"compiled (an empty target/classes dir or a coverage default is not "
                f"proof of compilation)"
            )
        elif (
            coverage_info is not None
            and _coverage_basis(coverage_info) == "none"
            and (not coverage_info["all_present"] or class_count == 0)
        ):
            # Plan 8 §3.4 (P2): no CLASS-weighted expectation could be derived,
            # so there is no fraction to grade — and the expectations that WERE
            # derived are not met. The check states that and the caller decides;
            # it never reads "nothing to measure" as "everything passed", which
            # is how p7d polaris earned "Built 100% of expected classes" from a
            # `class_coverage` that defaulted to 1.0.
            #
            # TWO independent questions, and each has its own arm. Round one
            # keyed the branch on "no class-based expectation" alone and a met
            # JAR expectation could not reach a full success; round two added
            # `and not all_present` and re-opened the zero-classes direction, so
            # a build that compiled NOTHING beside a jar on disk graded complete.
            # The table, entered on either trigger:
            #
            #  * nothing compiled -> BLOCKED, whether or not the derived
            #    expectations are met. This is the hard JVM gate (commons-chain,
            #    0 classes under a non-standard src layout); the
            #    `jvm_no_compiled_evidence` branch above catches only the case
            #    with no artifacts at all, and a checked-in or stale jar IS an
            #    artifact. Measuring compilation in `.class` files is a JVM-only
            #    contract, and this whole chain is JVM-only by construction: for
            #    any other build system `expected_artifacts` is `[]` (see where it
            #    is derived above), so `coverage_info` is None and no arm here is
            #    reachable. An extra `build_system in ("maven", "gradle")` test
            #    would be dead code that reads like a live guard.
            #  * classes compiled, an expectation unmet -> PARTIAL. Real output
            #    happened; how much of the project it is remains unknown, and the
            #    sentence names the artifacts that are missing rather than
            #    implying nothing was expected.
            #
            # A met expectation WITH classes compiled never reaches here: it is a
            # basis, it decided, and the Kotlin/Scala/Groovy module whose parsers
            # emit only the JAR expectation still gets its full success below.
            absent = coverage_info.get("missing") or []
            listed = ", ".join(absent[:5]) + (" ..." if len(absent) > 5 else "")
            named = f" ({listed})" if absent else ""
            if class_count > 0:
                success, complete = True, False
                reason = (
                    f"compiled {class_count:,} classes; {len(absent)} expected "
                    f"artifact(s) missing{named} and no class-based expectation "
                    f"could be derived — coverage has no basis"
                )
            elif coverage_info["all_present"]:
                success, complete = False, False
                present = ", ".join(coverage_info.get("found") or [])
                reason = (
                    f"No compiled .class files found for {build_system} build — the "
                    f"expected artifact(s) are present ({present}) but nothing compiled, "
                    f"and no class-based expectation could be derived; an existing JAR is "
                    f"not evidence this build compiled the project"
                )
            else:
                success, complete = False, False
                reason = (
                    f"No compiled .class files found for {build_system} build — nothing "
                    f"compiled, and no class-based expectation could be derived, so "
                    f"coverage has no basis"
                )
        elif coverage_info is not None:
            # `class_coverage` is ABSENT when nothing class-based was expected
            # (Plan 8 §3.4), and absent is not 1.0 — the old `.get(..., 1.0)`
            # default is what read "nothing to measure" as a met threshold. The
            # only way into this branch without a fraction is with every derived
            # expectation present, which decides on its own.
            coverage = coverage_info.get("class_coverage")
            missing = coverage_info.get("missing", [])
            if coverage_info["all_present"] or (coverage is not None and coverage >= threshold):
                success, complete = True, True
                reason = (
                    f"All expected build artifacts found: "
                    f"{', '.join(coverage_info['found'][:5])}"
                    if coverage_info["all_present"]
                    else (
                        f"Built {(coverage or 0) * 100:.0f}% of expected classes "
                        f"(>= {threshold * 100:.0f}% threshold)"
                    )
                )
            elif (coverage or 0) > 0 or has_real_output:
                # Real build output, but some ACTIVE modules did not compile —
                # honest PARTIAL, not a clean success.
                #
                # Counts, not a rounded percentage. Live ignite: 17,779 classes
                # compiled and 20 short across four modules rounded to "Built
                # 100% of expected classes (< 100% threshold)" — a sentence
                # that contradicts itself and tells the model nothing it can
                # act on.
                success, complete = True, False
                found_classes = coverage_info.get("classes_found", 0)
                expected_classes = coverage_info.get("classes_expected", 0)
                shortfall = max(expected_classes - found_classes, 0)
                reason = (
                    f"Built {found_classes} of {expected_classes} expected classes"
                    + (f", {shortfall} short" if shortfall else "")
                    + f" (below the {threshold * 100:.0f}% threshold); "
                    + f"{len(missing)} module(s) incomplete"
                    + (f": {', '.join(missing[:5])}" if missing else "")
                    + (" ..." if len(missing) > 5 else "")
                )
            else:
                success, complete = False, False
                reason = (
                    f"Only {(coverage or 0) * 100:.0f}% of expected classes built "
                    f"(< {threshold * 100:.0f}% threshold) — missing: "
                    f"{', '.join(missing[:8])}" + (" ..." if len(missing) > 8 else "")
                )

        elif evidence["has_build_fingerprints"]:
            # Fingerprints but no determinable per-module expectations (e.g. an
            # aggregator/empty root pom with no parseable modules or sources):
            # treat as a real, complete build (nothing to be incomplete against).
            #
            # STATED PLAINLY (Plan 8 §3.4, and NOT changed here): this — not the
            # branch above — is the arm where NO expectation of any kind could be
            # derived, so §3.4's own words ("basis none, classes > 0 -> PARTIAL;
            # classes = 0 -> BLOCKED") describe THIS branch, and this branch
            # returns a complete build. The consequence is exact: the "no
            # class-based expectation could be derived / coverage has no basis"
            # sentence above is unreachable for an input that derived NOTHING; it
            # only ever speaks for an input that derived a jar-or-file expectation
            # and no class expectation (which is the p7d polaris shape, and is why
            # polaris does reach it).
            #
            # Left at today's verdict deliberately, and measured rather than
            # assumed: routing `coverage_info is None` to partial/blocked for
            # maven/gradle turns three long-standing fences red — including
            # `test_validate_build_status_maven_unaffected` (commons-cli shape:
            # pom.xml, a jar, an empty target/classes, no derivable expectation)
            # and `test_build_validation_refs_prefer_artifact_samples`. That is a
            # verdict change for every single-module JVM project whose sources we
            # cannot enumerate, which is a spec decision and not this round's
            # finding. It is unchanged from main, so it is not a regression here.
            success, complete = True, True
            reason = f"Build fingerprints found for {build_system} project"

        elif evidence["has_artifacts"]:
            if build_system in ("maven", "gradle"):
                # A JVM build is green only with compiled .class evidence. A
                # stray/checked-in/vendored JAR is NOT proof the project compiled.
                if class_count > 0:
                    success, complete = True, True
                    reason = f"Found {class_count} compiled classes (build appears successful)"
                else:
                    success, complete = False, False
                    reason = (
                        f"No compiled .class files found for {build_system} build "
                        f"({evidence['artifact_count']} non-class artifact(s) such as vendored "
                        f"JARs are not evidence the project compiled)"
                    )
            else:
                # Non-JVM builds have no .class/JAR contract; existence of the
                # detected artifacts is the best available signal.
                success, complete = True, True
                reason = f"Found {evidence['artifact_count']} build artifacts"

        else:
            success, complete = False, False
            reason = "No build evidence found (no artifacts or build fingerprints)"

        # A rejected Maven module is uncertainty, not permission to shrink the
        # denominator. Safe records can still prove that a build happened, but
        # an incomplete reactor snapshot can never produce a full-green build.
        if (
            isinstance(maven_reactor_snapshot, _MavenReactorSnapshot)
            and not maven_reactor_snapshot.complete
            and terminal_reactor is None
        ):
            complete = False
            reactor_reason = (
                "Maven reactor could not be fully verified: " + maven_reactor_snapshot.reason
            )
            reason = f"{reason}; {reactor_reason}" if reason else reactor_reason

        # Plan 8 §3.5: the scan that knew is no longer commentary. When no
        # receipt outranks it, the scan OWNS the denominator, and a denominator
        # that counted a minority cannot also report a complete build — the
        # p7d polaris sentence ("Built 100% of expected classes · Module
        # coverage: 1/26 built") is unconstructible from here on. Under a
        # receipt-stated denominator the scan is deliberately not a cap: a
        # module the build never attempted is untried, not unbuilt, which is
        # the whole point of the #17 narrowing.
        if build_system in ("maven", "gradle") and terminal_reactor is None:
            if basis.states_a_shortfall or denominator_refusals:
                if complete:
                    # A completeness claim must not be left standing at the head
                    # of the sentence beside a minority count. What DECIDED goes
                    # first; what the earlier check found stays, subordinated,
                    # because it is still a fact (the derived expectation list
                    # really was met — it just did not cover the tree).
                    #
                    # The parenthetical is labelled with the check that ACTUALLY
                    # produced it. `coverage_info is None` means no expectation of
                    # any kind could be derived and no coverage check ran at all
                    # (the fingerprint and artifact branches); calling that
                    # sentence a coverage finding states something untrue about
                    # where it came from (Category 3).
                    if reason:
                        label = "coverage check" if coverage_info is not None else "build check"
                        found_clause = f" ({label}: {reason})"
                    else:
                        found_clause = ""
                    if basis.states_a_shortfall:
                        reason = (
                            f"Not a complete build — the module scan owns the denominator "
                            f"and {basis.built} of {basis.total} modules produced "
                            f"output{found_clause}"
                        )
                    else:
                        # §3.5's cap, reachable in EVERY state where the narrowing
                        # did not take effect — including one where the module scan
                        # is unavailable and cannot count a shortfall. The run
                        # recorded that it could not check its own denominator;
                        # that is uncertainty, and uncertainty is never complete.
                        stated = "; ".join(
                            _DENOMINATOR_REFUSALS.get(code, code)
                            for code in dict.fromkeys(denominator_refusals)
                        )
                        reason = (
                            f"Not a complete build — the coverage denominator could not be "
                            f"narrowed to what this run attempted: {stated}{found_clause}"
                        )
                complete = False
            reason = f"{reason} · {basis.phrase()}" if reason else basis.phrase()

        if terminal_reactor is not None:
            success, complete = True, True
            reason = (
                f"Root Maven {terminal_reactor['requested_action']} receipt "
                f"{terminal_reactor['receipt_id']} completed exactly with "
                f"{terminal_reactor['modules_succeeded']} of "
                f"{terminal_reactor['modules_total']} reactor modules successful; "
                "filesystem class and module scans are diagnostic"
            )

        # Surface the build command + timed duration (if a command tracker with a
        # recorded build is attached) and the primary artifact for the metrics
        # read model. validate_build_status itself does not execute the build, so
        # the duration is whatever maven_tool timed and stored on the tracker.
        command_tracker = getattr(self, "command_tracker", None)
        last_build = command_tracker.get_last_build_command() if command_tracker else None
        if last_build:
            command = last_build.get("command")
            if command:
                evidence["build_command"] = command
            dur = last_build.get("duration")
            if isinstance(dur, (int, float)):
                evidence["build_time"] = _format_build_duration(dur)
        # Primary artifact: first of the collected build-output samples.
        samples = evidence.get("artifact_samples") or []
        if samples:
            evidence.setdefault("artifact", samples[0])

        if not success:
            evidence_status = "blocked"
            conflicts = ["build_validation_failed"]
        elif not complete:
            evidence_status = "partial"
            conflicts = ["build_modules_incomplete"]
        else:
            evidence_status = "success"
            conflicts = []

        conflicts.extend(self._collect_env_conflicts())
        # A denominator the run could not check rides the verdict's OWN conflicts
        # channel, not only the evidence dict: the finalizer and the report kernel
        # cap on `result["conflicts"]`, and a disagreement that never reaches
        # there is a disagreement nothing downstream can act on.
        if terminal_reactor is None:
            for code in denominator_refusals:
                if code not in conflicts:
                    conflicts.append(code)
        if (
            isinstance(maven_reactor_snapshot, _MavenReactorSnapshot)
            and not maven_reactor_snapshot.complete
            and terminal_reactor is None
        ):
            conflicts.append("maven_reactor_unverified")
        if python_build is not None:
            conflicts.extend(python_build.get("metrics_conflicts") or [])

        result = {
            "success": success,
            "build_complete": complete,
            "evidence": evidence,
            "reason": reason,
            "evidence_status": evidence_status,
            "test_stats": None,
            "conflicts": conflicts,
            "evidence_refs": self._build_status_evidence_refs(project_dir, artifacts_result),
        }
        if python_build is not None and isinstance(python_build.get("test_entry_ready"), bool):
            result["test_entry_ready"] = python_build["test_entry_ready"]

        logger.info(f"Build validation complete: {evidence_status.upper()} - {reason}")
        return result

    def _collect_env_conflicts(self) -> List[str]:
        """jdk_mismatch / python_version_mismatch when the manifest's required
        toolchain differs from the active one at validation time.

        Report-only honesty signals (spec §4 + spec 2026-07-07 Component 4):
        provisioning failures degrade here instead of blocking the run. Empty
        when no requirement is known. python_version_mismatch is the exact
        mirror of jdk_mismatch, with one PythonPreflight-aligned tolerance: an
        active interpreter that satisfies the raw declared constraint is NOT a
        mismatch even when it differs from the resolved-newest version.
        """
        conflicts: List[str] = []
        try:
            from sag.runtime.env_overlay import EnvOverlayStore
            from sag.tools.internal.build_preflight import (
                active_java_major,
                read_live_build_requirements,
            )
            from sag.tools.internal.python_env import resolve_python_version

            manifest_read = read_live_build_requirements(self.docker_orchestrator)
            if (
                not manifest_read.complete
                or manifest_read.conflict is not None
                or manifest_read.payload is None
            ):
                return ["build_requirements_unavailable"]
            manifest = dict(manifest_read.payload)

            required_jdk = manifest.get("java_version")
            if required_jdk:
                active = active_java_major(self.docker_orchestrator)
                if active and active != str(required_jdk):
                    conflicts.append("jdk_mismatch")

            required_python = manifest.get("python_version")
            if required_python:
                # Reading the registered overlay is judge-safe. Executing
                # ``python3 --version`` is not: PATH may resolve a project venv,
                # and a physical judge must never launch project-owned code.
                candidate = (
                    EnvOverlayStore(self.docker_orchestrator).active_candidate("python") or {}
                )
                active_py = str(candidate.get("version") or "").strip() or None
                constraint = manifest.get("python_constraint")
                constraint_satisfied = bool(
                    active_py
                    and constraint
                    and resolve_python_version(constraint, [active_py]) == active_py
                )
                if active_py and active_py != str(required_python) and not constraint_satisfied:
                    conflicts.append("python_version_mismatch")
        except Exception as exc:
            logger.debug(f"env conflict check skipped: {exc}")
            conflicts.append("build_requirements_unavailable")
        return conflicts

    @staticmethod
    def _empty_python_build_result(
        *,
        test_entry_ready: Optional[bool] = None,
        reason: str = "",
        conflicts: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return {
            "venv_exists": False,
            "pip_check_clean": None,
            "distribution_record_ok": None,
            "distribution_origin_ok": None,
            "install_outcome": None,
            "imports_ok": None,
            "import_failures": [],
            "compileall_coverage": None,
            "compileall_metric_status": "unavailable",
            "compileall_source_count": 0,
            "compileall_compiled_source_count": 0,
            "compileall_foreign_pyc_count": 0,
            "compileall_cache_tag": None,
            "compileall_source_basis_sha256": None,
            "compileall_pyc_basis_sha256": None,
            "compileall_source_basis_entry_count": 0,
            "compileall_pyc_basis_entry_count": 0,
            "compileall_missing_sources": [],
            "compileall_foreign_pycs": [],
            "wheel_artifact_status": None,
            "wheel_artifacts": [],
            "wheel_artifacts_verified": None,
            "producer_receipt_status": "absent",
            "producer_receipt_ids": {},
            "metrics_conflicts": list(conflicts or ()),
            "ext_modules_ok": None,
            "native_artifact_ok": None,
            "test_entry_ready": test_entry_ready,
            "test_entry_candidate": None,
            "success": False,
            "complete": False,
            "reason": reason,
            "warnings": [reason] if reason else [],
        }

    @staticmethod
    def _normalized_python_evidence_root(value: Any) -> Optional[str]:
        """An absolute, newline-free POSIX path suitable for receipt binding."""

        raw = str(value or "").strip()
        if not raw.startswith("/") or "\x00" in raw or "\n" in raw:
            return None
        return posixpath.normpath(raw)

    @staticmethod
    def _python_producer_authority(
        receipt: Mapping[str, Any], observations: Mapping[str, Any]
    ) -> str:
        """Classify what a producer observation may prove.

        The runner outcome outranks the observation attached to its receipt. A
        failed/non-terminal runner may preserve an explicit negative fact (for
        example ``pip_check=broken``), but it may never launder a positive
        postcondition.  A clean terminal runner can support either direction.
        """

        if not _dispatch_terminated(receipt):
            return "unknown"
        operation = str(observations.get("operation") or "")
        detail = observations.get("setup" if operation == "setup_env" else operation)
        explicit_negative = False
        if isinstance(detail, Mapping):
            if operation == "setup_env":
                pip_check = detail.get("pip_check")
                imports = detail.get("imports")
                explicit_negative = bool(
                    detail.get("install_outcome") == "failed"
                    or (isinstance(pip_check, Mapping) and pip_check.get("status") == "broken")
                    or (
                        isinstance(imports, Mapping)
                        and imports.get("status") == "complete"
                        and int(imports.get("failed_count") or 0) > 0
                    )
                )
            elif operation == "compile":
                explicit_negative = detail.get("status") == "invalid"

        if explicit_negative:
            return "negative"
        project_steps = observations.get("recorded_project_steps")
        steps_green = (
            isinstance(project_steps, list)
            and bool(project_steps)
            and all(
                isinstance(step, Mapping) and step.get("outcome") == "completed"
                for step in project_steps
            )
        )
        if receipt.get("outcome") == "completed" and receipt.get("exit_code") == 0 and steps_green:
            return "positive"
        return "unknown"

    @staticmethod
    def _python_optional_pin(record: Mapping[str, Any], key: str) -> Optional[str]:
        if key not in record:
            return None
        value = record.get(key)
        if not isinstance(value, str):
            return ""
        return value.strip() or ""

    def _python_producer_contract_valid(
        self,
        receipt: Mapping[str, Any],
        *,
        operation: str,
        project_root: str,
        target_sha: str,
        survey_fingerprint: Optional[str],
        config_fingerprint: Optional[str],
        document_map_fingerprint: Optional[str],
        domain_id: Optional[str],
        fact_epoch: Optional[int],
    ) -> bool:
        """Bind a Python aggregate receipt to its frozen facade commitment.

        Python producer actions are dynamic, multi-command operations.  Their
        facade contract deliberately has no guessed argv.  That exact shape is
        a semantic contract only when the frozen public ``build`` call maps
        mechanically to the receipt operation and both sides explicitly omit
        argv compliance. Schema-v2 Python authority has no argv fallback; a
        future representation requires a new explicit binding version.
        """

        from sag.agent.evidence_assessments import contract_receipt_binding_problem
        from sag.agent.invocation_contracts import (
            PYTHON_FACADE_EXECUTION_BINDING,
            python_operation_for_public_action,
            read_frozen_contract,
        )

        contract_id = receipt.get("contract_id")
        receipt_hash = receipt.get("contract_hash")
        if not isinstance(contract_id, str) or not isinstance(receipt_hash, str):
            return False
        contract = read_frozen_contract(
            self.docker_orchestrator.execute_command,
            contract_id,
        )
        if not isinstance(contract, Mapping):
            return False
        if (
            contract_receipt_binding_problem(contract, receipt)
            or contract.get("execution_binding") != PYTHON_FACADE_EXECUTION_BINDING
            or "expected_argv" in contract
            or "compliance" in receipt
        ):
            return False

        normalized_root = self._normalized_python_evidence_root(project_root)
        expected_cwd = self._normalized_python_evidence_root(contract.get("expected_cwd"))
        working_cwd = self._normalized_python_evidence_root(receipt.get("working_directory"))
        actual_cwd = self._normalized_python_evidence_root(receipt.get("actual_cwd"))
        if not normalized_root or not (
            expected_cwd == working_cwd == actual_cwd == normalized_root
        ):
            return False
        if (
            str(contract.get("effective_action") or "").strip() != operation
            or str(receipt.get("requested_action") or "").strip() != operation
            or str(receipt.get("effective_action") or "").strip() != operation
        ):
            return False

        # Pins are absent-preserving. Two absent optional facts are equal;
        # an empty or one-sided key is a partial tuple and therefore invalid.
        current_target = str(target_sha or "").strip().lower()
        if not current_target:
            return False
        for record in (contract, receipt):
            recorded_target = self._python_optional_pin(record, "target_sha")
            if not isinstance(recorded_target, str) or recorded_target.lower() != current_target:
                return False
        for key, current_value in (
            ("survey_fingerprint", survey_fingerprint),
            ("config_fingerprint", config_fingerprint),
            ("document_map_fingerprint", document_map_fingerprint),
        ):
            current = str(current_value).strip() if current_value is not None else None
            for record in (contract, receipt):
                recorded = self._python_optional_pin(record, key)
                if current is None:
                    if recorded is not None:
                        return False
                elif recorded != current:
                    return False
        normalized_domain = self._normalized_python_evidence_root(domain_id) if domain_id else None
        for record in (contract, receipt):
            recorded_raw = self._python_optional_pin(record, "domain_id")
            recorded = (
                self._normalized_python_evidence_root(recorded_raw)
                if recorded_raw not in (None, "")
                else recorded_raw
            )
            if normalized_domain is None:
                if recorded is not None:
                    return False
            elif recorded != normalized_domain:
                return False
        for record in (contract, receipt):
            if fact_epoch is None:
                if "fact_epoch" in record:
                    return False
            elif record.get("fact_epoch") != fact_epoch:
                return False

        requested = contract.get("requested_call")
        if not isinstance(requested, Mapping) or requested.get("tool") != "build":
            return False
        params = requested.get("params")
        if not isinstance(params, Mapping):
            return False
        requested_root = self._normalized_python_evidence_root(params.get("working_directory"))
        if (
            python_operation_for_public_action(params.get("action")) != operation
            or requested_root != normalized_root
        ):
            return False
        if operation in {"build", "compile"} and params.get("args") not in (None, ""):
            return False
        # Non-empty Python dependency targets are deliberately disabled until
        # the facade and judge share one typed PolicyClaim verifier. An opaque
        # supporting id is not install authority, and the ordinary empty-args
        # deps action still installs the project's declared dependency set.
        if operation == "setup_env" and str(params.get("args") or "").strip():
            return False
        return True

    def _read_python_producer_receipts(
        self,
        project_root: str,
        manifest: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Read hash-verified Python producer observations for this checkout.

        Receipt files are the durable authority. ToolResult prose, a raw
        ``runner_dispatched`` bit, old-run files, other checkouts, and sibling
        domains are inert. A malformed current stream is uncertainty for the
        entire read: accepting an older valid line beside a clipped newer one
        would refine unknown evidence upward.
        """

        empty: Dict[str, Any] = {
            "status": "absent",
            "operations": {},
            "conflicts": [],
            "warnings": [],
        }
        receipt_records = self._read_live_invocation_receipts()
        if receipt_records == []:
            return empty
        if receipt_records is None:
            return {
                **empty,
                "status": "unreadable",
                "conflicts": ["python_producer_receipts_unreadable"],
                "warnings": ["Python producer receipt stream could not be read"],
            }

        from sag.agent.attempt_policy import (
            current_run_durable_receipt,
            resolve_current_build_receipt_scope,
        )
        from sag.agent.invocation_receipts import (
            RECEIPT_SCHEMA_VERSION,
            active_receipt_run_id,
            nearest_domain_fact_epoch,
            nearest_domain_root,
            producer_observations_sha256,
            producer_sequence_from_receipt_id,
            python_import_targets,
            read_producer_observations,
        )

        current_run_id = self.receipt_run_id or active_receipt_run_id()
        normalized_root = self._normalized_python_evidence_root(project_root)
        scope = resolve_current_build_receipt_scope(
            self.docker_orchestrator,
            run_id=current_run_id,
            workspace_root=self.project_path,
            project_root=normalized_root,
        )
        if normalized_root is None or not scope.available:
            return {
                **empty,
                "status": "scope_unavailable",
                "conflicts": ["python_producer_receipt_scope_unavailable"],
                "warnings": [
                    "Python producer receipts could not be bound to the current run, "
                    "checkout, and project root"
                ],
            }
        records: List[Mapping[str, Any]] = list(receipt_records)

        survey = manifest.get("survey") if isinstance(manifest, Mapping) else None
        manifest_target = None
        if isinstance(survey, Mapping):
            manifest_target = str(survey.get("target_sha") or "").strip().lower() or None
        if manifest_target is None and isinstance(manifest, Mapping):
            manifest_target = str(manifest.get("target_sha") or "").strip().lower() or None
        if manifest_target != str(scope.target_sha or "").strip().lower():
            return {
                **empty,
                "status": "invalid",
                "conflicts": ["python_producer_receipt_invalid"],
                "warnings": ["Python producer manifest target disagrees with the run pin"],
            }
        manifest_config = None
        if isinstance(survey, Mapping) and "config_fingerprint" in survey:
            value = survey.get("config_fingerprint")
            manifest_config = str(value).strip() if isinstance(value, str) and value.strip() else ""
        elif isinstance(manifest, Mapping) and "config_fingerprint" in manifest:
            value = manifest.get("config_fingerprint")
            manifest_config = str(value).strip() if isinstance(value, str) and value.strip() else ""
        if manifest_config == "":
            return {
                **empty,
                "status": "invalid",
                "conflicts": ["python_producer_receipt_invalid"],
                "warnings": ["Python producer manifest config fingerprint is partial"],
            }
        manifest_survey = None
        if isinstance(survey, Mapping) and "survey_fingerprint" in survey:
            value = survey.get("survey_fingerprint")
            manifest_survey = str(value).strip() if isinstance(value, str) and value.strip() else ""
        elif isinstance(manifest, Mapping) and "survey_fingerprint" in manifest:
            value = manifest.get("survey_fingerprint")
            manifest_survey = str(value).strip() if isinstance(value, str) and value.strip() else ""
        manifest_document_map = None
        if isinstance(survey, Mapping) and "document_map_fingerprint" in survey:
            value = survey.get("document_map_fingerprint")
            manifest_document_map = (
                str(value).strip() if isinstance(value, str) and value.strip() else ""
            )
        elif isinstance(manifest, Mapping) and "document_map_fingerprint" in manifest:
            value = manifest.get("document_map_fingerprint")
            manifest_document_map = (
                str(value).strip() if isinstance(value, str) and value.strip() else ""
            )
        if manifest_survey == "" or manifest_document_map == "":
            return {
                **empty,
                "status": "invalid",
                "conflicts": ["python_producer_receipt_invalid"],
                "warnings": ["Python producer manifest fingerprint tuple is partial"],
            }
        manifest_domain = nearest_domain_root(manifest, normalized_root)
        manifest_fact_epoch = nearest_domain_fact_epoch(manifest, normalized_root)
        manifest_import_targets = python_import_targets(manifest)

        candidates: Dict[str, List[Tuple[Mapping[str, Any], Mapping[str, Any]]]] = {
            "setup_env": [],
            "build": [],
            "compile": [],
        }
        seen_sequences: Dict[int, str] = {}
        for receipt in records:
            receipt_id = str(receipt.get("receipt_id") or "").strip()
            if not current_run_durable_receipt(
                receipt,
                receipt_id=receipt_id,
                run_id=current_run_id,
                target_sha=scope.target_sha or "",
                project_root=normalized_root,
            ):
                continue
            if str(receipt.get("tool") or "").strip().lower() != "python":
                continue
            # What physically ran, with no requested-action fallback behind it:
            # `_read_live_invocation_receipts` admits only `validate_receipt_v2`
            # records, and that schema requires a non-empty `effective_action`.
            operation = str(receipt.get("effective_action") or "").strip()
            if operation not in candidates:
                continue
            if type(receipt.get("schema_version")) is not int or (
                receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
            ):
                return {
                    **empty,
                    "status": "invalid",
                    "conflicts": ["python_producer_receipt_invalid"],
                    "warnings": [f"Python producer receipt {receipt_id} has unsupported schema"],
                }
            actual_cwd = self._normalized_python_evidence_root(receipt.get("actual_cwd"))
            raw_domain_id = str(receipt.get("domain_id") or "").strip()
            domain_id = (
                self._normalized_python_evidence_root(raw_domain_id) if raw_domain_id else None
            )
            # Python operations are rooted actions. Containment alone would let
            # a sibling/nested domain's receipt grade this root. A single-domain
            # survey intentionally omits ``build_domains`` and therefore writes
            # no domain_id; exact actual_cwd still binds that compatibility case.
            if actual_cwd != normalized_root or (raw_domain_id and domain_id != normalized_root):
                continue
            sequence = receipt.get("producer_sequence")
            derived_sequence = producer_sequence_from_receipt_id(receipt_id)
            if (
                type(sequence) is not int
                or sequence <= 0
                or derived_sequence is None
                or sequence != derived_sequence
            ):
                return {
                    **empty,
                    "status": "invalid",
                    "conflicts": ["python_producer_receipt_invalid"],
                    "warnings": [f"Python producer receipt {receipt_id} has no bound sequence"],
                }
            previous_id = seen_sequences.setdefault(sequence, receipt_id)
            if previous_id != receipt_id:
                return {
                    **empty,
                    "status": "invalid",
                    "conflicts": ["python_producer_sequence_conflict"],
                    "warnings": [f"Python producer sequence {sequence} names multiple receipts"],
                }
            if not self._python_producer_contract_valid(
                receipt,
                operation=operation,
                project_root=normalized_root,
                target_sha=scope.target_sha or "",
                survey_fingerprint=manifest_survey,
                config_fingerprint=manifest_config,
                document_map_fingerprint=manifest_document_map,
                domain_id=manifest_domain,
                fact_epoch=manifest_fact_epoch,
            ):
                return {
                    **empty,
                    "status": "invalid",
                    "conflicts": ["python_producer_receipt_invalid"],
                    "warnings": [
                        f"Python producer receipt {receipt_id} has invalid contract authority"
                    ],
                }
            observations = read_producer_observations(receipt)
            if observations is None:
                return {
                    **empty,
                    "status": "invalid",
                    "conflicts": ["python_producer_receipt_invalid"],
                    "warnings": [
                        f"Python producer receipt {receipt_id} failed schema/hash validation"
                    ],
                }
            if observations.get("operation") != operation:
                return {
                    **empty,
                    "status": "invalid",
                    "conflicts": ["python_producer_receipt_invalid"],
                    "warnings": [
                        f"Python producer receipt {receipt_id} action/observation disagrees"
                    ],
                }
            candidates[operation].append((receipt, observations))

        selected: Dict[str, Dict[str, Any]] = {}
        warnings: List[str] = []
        for operation, options in candidates.items():
            if not options:
                continue
            receipt, observations = max(options, key=lambda item: item[0]["producer_sequence"])
            authority = self._python_producer_authority(receipt, observations)
            if operation == "setup_env" and authority == "positive":
                setup = observations.get("setup")
                imports = setup.get("imports") if isinstance(setup, Mapping) else None
                expected_target_hash = (
                    producer_observations_sha256(manifest_import_targets)
                    if manifest_import_targets
                    else ""
                )
                if (
                    not manifest_import_targets
                    or not isinstance(imports, Mapping)
                    or imports.get("status") != "complete"
                    or imports.get("targets") != manifest_import_targets
                    or str(imports.get("targets_sha256") or "") != expected_target_hash
                ):
                    authority = "unknown"
                    warnings.append(
                        "Python setup producer receipt cannot support positive facts: "
                        "its import target set is not the current mechanical manifest set"
                    )
            selected[operation] = {
                "receipt": receipt,
                "observations": observations,
                "authority": authority,
            }
            if authority == "unknown":
                warnings.append(
                    f"Python {operation} producer receipt cannot support positive facts: "
                    "its runner did not complete successfully"
                )
        return {
            "status": "available",
            "operations": selected,
            "conflicts": [],
            "warnings": warnings,
        }

    def _execute_python_readonly_probe(self, command: str) -> Optional[str]:
        """Execute a read-only filesystem probe without output truncation."""

        try:
            try:
                result = self.docker_orchestrator.execute_command(
                    command,
                    workdir=None,
                    timeout=120,
                    truncate_output=False,
                )
            except TypeError as exc:
                detail = str(exc)
                if not any(name in detail for name in ("truncate_output", "workdir", "timeout")):
                    raise
                result = self.docker_orchestrator.execute_command(command)
        except Exception as exc:
            logger.debug(f"Python read-only evidence probe failed: {exc}")
            return None
        if _command_did_not_run(result) or not json_record_stream_succeeded(result):
            return None
        return str((result or {}).get("output") or "")

    def _verify_python_wheel_delta(
        self, project_root: str, observation: Mapping[str, Any]
    ) -> Tuple[Optional[bool], List[Dict[str, Any]]]:
        """Re-hash a receipt's bounded wheel delta without executing Python."""

        status = observation.get("artifact_status")
        artifacts = observation.get("artifacts") or []
        if status != "produced":
            return (False if status in {"missing", "stale_only"} else None), []
        verified: List[Dict[str, Any]] = []
        normalized_root = posixpath.normpath(project_root)
        for artifact in artifacts:
            if not isinstance(artifact, Mapping):
                return None, []
            relative = str(artifact.get("path") or "")
            candidate = posixpath.normpath(posixpath.join(normalized_root, relative))
            if not candidate.startswith(normalized_root.rstrip("/") + "/"):
                return None, []
            digest_output = self._execute_python_readonly_probe(
                f"sha256sum -- {shlex.quote(candidate)}"
            )
            size_output = self._execute_python_readonly_probe(f"wc -c < {shlex.quote(candidate)}")
            if digest_output is None or size_output is None:
                return None, []
            digest_line = digest_output.strip().splitlines()
            if len(digest_line) != 1 or not re.fullmatch(r"[0-9a-fA-F]{64}  .+", digest_line[0]):
                return None, []
            try:
                observed_size = int(size_output.strip())
            except (TypeError, ValueError):
                return None, []
            if (
                digest_line[0][:64].lower() != artifact.get("sha256")
                or posixpath.normpath(digest_line[0][66:]) != candidate
                or observed_size != artifact.get("size_bytes")
            ):
                return False, []
            verified.append(dict(artifact))
        return True, verified

    def _verify_python_compile_basis(
        self, project_root: str, observation: Mapping[str, Any]
    ) -> str:
        """Reproduce source/PYC basis hashes from read-only filesystem facts.

        ``verified`` means every count, basis hash, sample, and conflict still
        matches the immutable observation. ``changed`` means the current tree
        differs. ``unreadable`` means the basis could not be reproduced and
        therefore remains unknown.
        """

        if observation.get("status") not in {"valid", "invalid"}:
            return "unreadable"
        normalized_root = posixpath.normpath(project_root)
        roots: List[str] = []
        for relative in observation.get("roots") or ():
            candidate = posixpath.normpath(posixpath.join(normalized_root, str(relative)))
            if candidate != normalized_root and not candidate.startswith(
                normalized_root.rstrip("/") + "/"
            ):
                return "unreadable"
            roots.append(candidate)
        if not roots:
            return "unreadable"
        quoted_roots = " ".join(shlex.quote(root) for root in roots)
        directories = self._execute_python_readonly_probe(
            "test " + " -a ".join(f"-d {shlex.quote(root)}" for root in roots)
        )
        if directories is None:
            return "unreadable"
        links = self._execute_python_readonly_probe(f"find -P {quoted_roots} -type l -print -quit")
        if links is None or links.strip():
            # The producer resolves symlinks. Reconstructing that graph in a
            # shell parser is not exact, so preserve unknown instead.
            return "unreadable"
        listing = self._execute_python_readonly_probe(
            f"find -P {quoted_roots} -mindepth 1 "
            "\\( -type d \\( -name '.*' -o -name tests -o -name docs -o -name examples "
            "\\) -prune \\) -o "
            "\\( -type f ! -name '.*' \\( -name '*.py' -o -name '*.pyc' \\) "
            "-exec sha256sum -- {} + \\)"
        )
        if listing is None:
            return "unreadable"
        files: Dict[str, str] = {}
        for line in listing.splitlines():
            if not re.fullmatch(r"[0-9a-fA-F]{64}  /[^\x00\n]+", line):
                return "unreadable"
            digest = line[:64].lower()
            path = posixpath.normpath(line[66:])
            if not any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots):
                return "unreadable"
            previous = files.setdefault(path, digest)
            if previous != digest:
                return "unreadable"

        sources = {path: digest for path, digest in files.items() if path.endswith(".py")}
        pycs = {path: digest for path, digest in files.items() if path.endswith(".pyc")}
        cache_tag = str(observation.get("cache_tag") or "")
        expected = {
            posixpath.join(
                posixpath.dirname(source),
                "__pycache__",
                f"{posixpath.basename(source)[:-3]}.{cache_tag}.pyc",
            ): source
            for source in sources
        }
        mapped: Dict[str, str] = {}
        for pyc in pycs:
            source = expected.get(pyc)
            if source is None and posixpath.basename(posixpath.dirname(pyc)) == "__pycache__":
                marker = f".{cache_tag}"
                filename = posixpath.basename(pyc)
                marker_index = filename.rfind(marker)
                if marker_index > 0 and filename.endswith(".pyc"):
                    candidate = posixpath.join(
                        posixpath.dirname(posixpath.dirname(pyc)),
                        f"{filename[:marker_index]}.py",
                    )
                    if candidate in sources:
                        source = candidate
            if source is not None:
                mapped[pyc] = source
        compiled_pairs = {
            expected_pyc: source
            for expected_pyc, source in expected.items()
            if expected_pyc in pycs
        }
        compiled_pairs.update(mapped)
        compiled_sources = set(compiled_pairs.values())
        missing_sources = sorted(set(sources) - compiled_sources)
        foreign_pycs = sorted(set(pycs) - set(mapped))
        source_basis = [{"path": path, "sha256": sources[path]} for path in sorted(sources)]
        pyc_basis = [
            {"path": path, "sha256": pycs[path], "source": compiled_pairs[path]}
            for path in sorted(compiled_pairs)
        ]
        status = "invalid" if foreign_pycs else ("valid" if sources else "unavailable")
        coverage = len(compiled_sources) / len(sources) if sources and not foreign_pycs else None
        conflicts = ["metrics_conflict"] if foreign_pycs else []
        current = {
            "status": status,
            "source_count": len(sources),
            "compiled_source_count": len(compiled_sources),
            "missing_source_count": len(missing_sources),
            "foreign_pyc_count": len(foreign_pycs),
            "coverage": coverage,
            "source_basis_sha256": hashlib.sha256(
                json.dumps(
                    source_basis,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
            "pyc_basis_sha256": hashlib.sha256(
                json.dumps(
                    pyc_basis,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
            "source_basis_entry_count": len(source_basis),
            "pyc_basis_entry_count": len(pyc_basis),
            "missing_sources": [
                posixpath.relpath(path, normalized_root) for path in missing_sources[:20]
            ],
            "foreign_pycs": [
                posixpath.relpath(path, normalized_root) for path in foreign_pycs[:20]
            ],
            "conflicts": conflicts,
        }
        comparable_keys = tuple(current)
        for key in comparable_keys:
            expected_value = observation.get(key)
            current_value = current[key]
            if key == "coverage" and expected_value is not None and current_value is not None:
                if abs(float(expected_value) - float(current_value)) <= 1e-12:
                    continue
            if expected_value != current_value:
                return "changed"
        return "verified"

    def _verify_python_build(self, project_dir: str) -> Dict[str, any]:
        """Read-only Python build evidence.

        The survey and filesystem may prove a venv, exact installed
        distribution ownership, declared extension artifacts, and native
        artifacts. Dependency integrity, importability, and bytecode
        compilation come only from a current run/target/root-bound producer
        receipt whose typed observation still matches a read-only filesystem
        basis. Missing, stale, malformed, or unrepeatable observations remain
        explicitly unknown.

        In particular this method never executes a venv/project-owned binary,
        imports project code, runs ``pip check``, or writes/counts ``.pyc`` it
        created itself.
        """
        from sag.tools.internal.build_preflight import read_live_build_requirements

        manifest_read = read_live_build_requirements(self.docker_orchestrator)
        if (
            not manifest_read.complete
            or manifest_read.conflict is not None
            or manifest_read.payload is None
        ):
            return self._empty_python_build_result(
                reason=(
                    "Host-published build requirements are unavailable; Python "
                    "build evidence is unobserved"
                ),
                conflicts=["build_requirements_unavailable"],
            )
        manifest = dict(manifest_read.payload)
        venv = manifest.get("python_venv") or f"{project_dir.rstrip('/')}/.venv"
        packages = manifest.get("python_packages") or []
        survey = manifest.get("survey")
        if not isinstance(survey, dict):
            survey = {}
        try:
            facts_version = int(
                survey.get("analyzer_version") or manifest.get("analyzer_version") or 0
            )
        except (TypeError, ValueError):
            facts_version = 0
        distribution_name = str(manifest.get("python_distribution_name") or "").strip()
        strict_distribution = facts_version >= 8
        survey_root = str(
            survey.get("project_path")
            or manifest.get("python_root")
            or manifest.get("build_root")
            or project_dir
        ).rstrip("/")
        install_root = str(manifest.get("python_root") or survey_root).rstrip("/")
        normalized_survey_root = posixpath.normpath(survey_root)
        normalized_install_root = posixpath.normpath(install_root)
        install_root_is_scoped = normalized_install_root == normalized_survey_root or (
            normalized_install_root.startswith(f"{normalized_survey_root}/")
        )
        package_paths = (manifest.get("python_package_paths") or []) if strict_distribution else []
        result: Dict[str, Any] = self._empty_python_build_result(
            test_entry_ready=(
                False if strict_distribution and manifest.get("has_native_build") else None
            )
        )

        venv_probe = self._execute_command_with_logging(f"test -d {venv}", "checking python venv")
        result["venv_exists"] = venv_probe["success"]
        if not result["venv_exists"]:
            result["reason"] = f"No venv at {venv} — the Python environment was never set up"
            return result

        if strict_distribution and not distribution_name:
            # A facts-v8 setup.py/setup.cfg project may have no statically
            # observable distribution name.  Fail CLOSED on exact ownership:
            # never fall through to the legacy broad record scan, but preserve
            # the real evidence that a venv exists as PARTIAL rather than
            # inventing a hard setup failure.
            result["success"] = True
            result["reason"] = (
                "Facts-v8 exact distribution ownership unverifiable: manifest "
                "is missing python_distribution_name"
            )
            result["warnings"].append(result["reason"])
            return result

        if strict_distribution and not install_root_is_scoped:
            result["reason"] = (
                f"Surveyed Python install root {install_root} escapes repository root "
                f"{survey_root}"
            )
            return result

        if strict_distribution and manifest.get("has_native_build"):
            smoke_candidate = self._verified_smoke_candidate(
                survey_root,
                manifest.get("python_smoke_candidates") or [],
            )
            result["test_entry_candidate"] = smoke_candidate
            result["test_entry_ready"] = smoke_candidate is not None

        # Facts-v8 names the distribution observed by the survey. That exact
        # record is the only record allowed to prove the root project was
        # installed. In particular, a local provider such as apache-tvm-ffi
        # must never stand in for the root apache-tvm distribution.
        if strict_distribution:
            strict_record = self._strict_installed_distribution(
                venv,
                install_root,
                distribution_name,
                survey_root=survey_root,
            )
            result["distribution_record_ok"] = strict_record["record_ok"]
            result["distribution_origin_ok"] = strict_record["origin_ok"]
            if not strict_record["record_ok"]:
                result["reason"] = strict_record["reason"]
                return result
            installed = strict_record["packages"]
            declared_path_imports = self._manifest_package_imports(package_paths)
            if declared_path_imports:
                packages = declared_path_imports
                if installed:
                    unowned = sorted(name for name in packages if name not in installed)
                    if unowned:
                        result["reason"] = (
                            "Survey package path import(s) are absent from the exact "
                            f"{distribution_name} installed record: {', '.join(unowned)}"
                        )
                        return result
            elif installed:
                packages = installed
        else:
            # Legacy manifest compatibility: keep the historical project-record
            # ladder for surveys that predate an exact distribution fact.
            installed = self._installed_top_level_packages(venv, project_dir)

        # Installed top-level records remain useful ownership facts. They name
        # what the exact project distribution says it installed, but are never
        # treated as proof that importing those names succeeds.
        if packages and installed:
            junk = sorted(name for name in packages if name not in installed)
            if junk:
                result["warnings"].append(
                    "discovered but not installed: "
                    + ", ".join(junk)
                    + " — skipped by the imports rung (not in the project's "
                    "installed record)"
                )
            if not strict_distribution:
                packages = installed
        elif not packages:
            # Empty manifest (package_dir layouts, bug #6): the installed
            # record is the fallback source of import targets.
            packages = installed

        producer_view = self._read_python_producer_receipts(install_root, manifest)
        result["producer_receipt_status"] = producer_view["status"]
        result["metrics_conflicts"].extend(producer_view.get("conflicts") or ())
        result["warnings"].extend(producer_view.get("warnings") or ())
        producer_operations = producer_view.get("operations") or {}
        result["producer_receipt_ids"] = {
            operation: str(entry["receipt"].get("receipt_id") or "")
            for operation, entry in producer_operations.items()
            if isinstance(entry, Mapping) and isinstance(entry.get("receipt"), Mapping)
        }

        setup_authoritative = False
        setup_failed = False
        setup_entry = producer_operations.get("setup_env")
        if isinstance(setup_entry, Mapping):
            setup_observations = setup_entry.get("observations") or {}
            setup = setup_observations.get("setup")
            authority = setup_entry.get("authority")
            if isinstance(setup, Mapping) and posixpath.normpath(
                str(setup.get("venv") or "")
            ) == posixpath.normpath(venv):
                if authority == "positive" and setup.get("install_outcome") == "success":
                    setup_authoritative = True
                    result["install_outcome"] = "success"
                    pip_check = setup.get("pip_check")
                    if isinstance(pip_check, Mapping):
                        result["pip_check_clean"] = pip_check.get("status") == "clean"
                    imports = setup.get("imports")
                    if isinstance(imports, Mapping) and imports.get("status") == "complete":
                        result["imports_ok"] = int(imports.get("failed_count") or 0) == 0
                        result["import_failures"] = [
                            str(failure.get("target") or "")
                            for failure in imports.get("failures") or ()
                            if isinstance(failure, Mapping) and failure.get("target")
                        ]
                    elif isinstance(imports, Mapping):
                        result["warnings"].append(
                            "package importability unknown: producer receipt records "
                            f"{imports.get('reason_code') or 'unavailable'}"
                        )
                elif authority == "negative":
                    if setup.get("install_outcome") == "failed":
                        result["install_outcome"] = "failed"
                        setup_failed = True
                    pip_check = setup.get("pip_check")
                    if isinstance(pip_check, Mapping) and pip_check.get("status") == "broken":
                        result["pip_check_clean"] = False
                        setup_failed = True
                    imports = setup.get("imports")
                    if (
                        isinstance(imports, Mapping)
                        and imports.get("status") == "complete"
                        and int(imports.get("failed_count") or 0) > 0
                    ):
                        result["imports_ok"] = False
                        result["import_failures"] = [
                            str(failure.get("target") or "")
                            for failure in imports.get("failures") or ()
                            if isinstance(failure, Mapping) and failure.get("target")
                        ]
                        setup_failed = True
            else:
                result["metrics_conflicts"].append("python_setup_receipt_venv_mismatch")
                result["warnings"].append(
                    "Python setup producer receipt names a different virtual environment"
                )

        package_note = ", ".join(packages[:5]) if packages else "no project-owned names recorded"
        if result["pip_check_clean"] is None:
            result["warnings"].append(
                "pip dependency integrity unknown: no producer receipt; "
                "physical judge did not execute pip"
            )
        if result["imports_ok"] is None:
            result["warnings"].append(
                "package importability unknown: no producer receipt; physical judge did not import "
                + package_note
            )

        compile_entry = producer_operations.get("compile")
        if isinstance(compile_entry, Mapping):
            compile_observations = compile_entry.get("observations") or {}
            compile_observation = compile_observations.get("compile")
            authority = compile_entry.get("authority")
            if isinstance(compile_observation, Mapping):
                metric_status = compile_observation.get("status")
                can_consume = (authority == "positive" and metric_status == "valid") or (
                    authority == "negative" and metric_status == "invalid"
                )
                if can_consume:
                    basis_status = self._verify_python_compile_basis(
                        install_root, compile_observation
                    )
                    if basis_status == "verified":
                        result["compileall_metric_status"] = metric_status
                        result["compileall_coverage"] = compile_observation.get("coverage")
                        result["compileall_source_count"] = compile_observation["source_count"]
                        result["compileall_compiled_source_count"] = compile_observation[
                            "compiled_source_count"
                        ]
                        result["compileall_foreign_pyc_count"] = compile_observation[
                            "foreign_pyc_count"
                        ]
                        result["compileall_cache_tag"] = compile_observation["cache_tag"]
                        result["compileall_source_basis_sha256"] = compile_observation[
                            "source_basis_sha256"
                        ]
                        result["compileall_pyc_basis_sha256"] = compile_observation[
                            "pyc_basis_sha256"
                        ]
                        result["compileall_source_basis_entry_count"] = compile_observation[
                            "source_basis_entry_count"
                        ]
                        result["compileall_pyc_basis_entry_count"] = compile_observation[
                            "pyc_basis_entry_count"
                        ]
                        result["compileall_missing_sources"] = list(
                            compile_observation.get("missing_sources") or ()
                        )
                        result["compileall_foreign_pycs"] = list(
                            compile_observation.get("foreign_pycs") or ()
                        )
                        result["metrics_conflicts"].extend(
                            compile_observation.get("conflicts") or ()
                        )
                    else:
                        conflict = (
                            "python_compile_basis_changed"
                            if basis_status == "changed"
                            else "python_compile_basis_unreadable"
                        )
                        result["metrics_conflicts"].append(conflict)
                        result["warnings"].append(
                            "bytecode compilation unknown: current filesystem basis "
                            + (
                                "changed after the receipt"
                                if basis_status == "changed"
                                else "could not be read"
                            )
                        )
                elif metric_status == "unavailable":
                    result["warnings"].append(
                        "bytecode compilation unknown: producer receipt records "
                        f"{compile_observation.get('reason_code') or 'unavailable'}"
                    )
        if result["compileall_metric_status"] == "unavailable" and not any(
            warning.startswith("bytecode compilation unknown:") for warning in result["warnings"]
        ):
            result["warnings"].append(
                "bytecode compilation unknown: no producer receipt; "
                "physical judge did not run compileall"
            )

        build_entry = producer_operations.get("build")
        if isinstance(build_entry, Mapping):
            build_observations = build_entry.get("observations") or {}
            build_observation = build_observations.get("build")
            authority = build_entry.get("authority")
            if isinstance(build_observation, Mapping) and authority in {"positive", "negative"}:
                result["wheel_artifact_status"] = build_observation.get("artifact_status")
                verified, artifacts = self._verify_python_wheel_delta(
                    install_root, build_observation
                )
                result["wheel_artifacts_verified"] = verified
                result["wheel_artifacts"] = artifacts
                if verified is False and build_observation.get("artifact_status") == "produced":
                    result["warnings"].append(
                        "wheel artifact delta changed after its producer receipt"
                    )
                elif verified is None and build_observation.get("artifact_status") == "produced":
                    result["warnings"].append(
                        "wheel artifact delta could not be re-read from the filesystem"
                    )

        so_missing = False
        if manifest.get("has_c_extensions"):
            so_probe = self._execute_command_with_logging(
                f"find {install_root if strict_distribution else project_dir} {venv} "
                "-name '*.so' -type f 2>/dev/null | head -1",
                "C-extension artifacts",
            )
            result["ext_modules_ok"] = bool((so_probe.get("output") or "").strip())
            so_missing = not result["ext_modules_ok"]

        # Native-core rung (live TVM, root CMakeLists.txt): the python package
        # cannot import without its native library (libtvm.so). When the manifest
        # flags has_native_build, look for a BUILT native artifact (.so/.dylib)
        # under the package dir or a build/ tree — its ABSENCE caps the build at
        # PARTIAL ("native core not built"), NEVER a hard block: pure-python
        # parts and tests may still run.
        native_missing = False
        if manifest.get("has_native_build"):
            if strict_distribution:
                # Facts-v8 records the native output locations actually derived
                # from the build metadata. Do not scan the repo or venv: a
                # dependency's numpy/tvm-ffi .so is not root-project evidence.
                search_roots = self._manifest_relative_roots(
                    survey_root,
                    manifest.get("native_artifact_roots") or [],
                )
            else:
                # Legacy manifest compatibility: old surveys had no grounded
                # native_artifact_roots, so retain their broad best-effort scan.
                search_roots = {
                    project_dir.rstrip("/"),
                    venv.rstrip("/"),
                }
                repo_root = project_dir.rstrip("/").rsplit("/", 1)[0]
                if repo_root and repo_root != project_dir.rstrip("/"):
                    search_roots.add(f"{repo_root}/build")
            if search_roots:
                native_probe = self._execute_command_with_logging(
                    "find "
                    + " ".join(shlex.quote(path) for path in sorted(search_roots))
                    + " \\( -name '*.so' -o -name '*.dylib' \\) "
                    + "-type f 2>/dev/null"
                    + ("" if strict_distribution else " | head -1"),
                    "native core artifacts",
                )
                candidates = [
                    line.strip()
                    for line in (native_probe.get("output") or "").splitlines()
                    if line.strip()
                ]
                result["native_artifact_ok"] = (
                    self._verified_native_artifact(
                        candidates,
                        search_roots,
                        survey_root,
                    )
                    if strict_distribution
                    else bool(candidates)
                )
            else:
                result["native_artifact_ok"] = False
                result["warnings"].append(
                    "native artifact rung has no safe survey-derived output root"
                )
            native_missing = not result["native_artifact_ok"]
            if strict_distribution and result["native_artifact_ok"]:
                result["test_entry_ready"] = True

        partial_reasons = []
        if result["pip_check_clean"] is None:
            partial_reasons.append("pip dependency integrity unknown: no producer receipt")
        elif result["pip_check_clean"] is False:
            partial_reasons.append("pip dependency check reported broken requirements")
        if result["imports_ok"] is None:
            partial_reasons.append("package importability unknown: no producer receipt")
        elif result["imports_ok"] is False:
            partial_reasons.append(
                "project package import failed"
                + (
                    f": {', '.join(result['import_failures'][:5])}"
                    if result["import_failures"]
                    else ""
                )
            )
        if result["compileall_metric_status"] == "unavailable":
            partial_reasons.append("bytecode compilation unknown: no producer receipt")
        elif result["compileall_metric_status"] == "invalid":
            partial_reasons.append("bytecode compilation basis is invalid")
        elif (result["compileall_coverage"] or 0) < 1.0:
            partial_reasons.append(
                f"bytecode compilation covered {(result['compileall_coverage'] or 0) * 100:.0f}% "
                "of current sources"
            )
        if so_missing:
            partial_reasons.append("declared C-extensions have no built .so artifact")
        if native_missing:
            # Live TVM: root CMakeLists.txt native core with no built .so/.dylib.
            # PARTIAL, never a block — the python package will not fully import,
            # but pure-python parts and tests may still run.
            partial_reasons.append("native core not built")
        if setup_failed:
            result["success"] = False
            result["reason"] = "; ".join(partial_reasons) or "Python setup producer failed"
            return result

        runtime_complete = bool(
            setup_authoritative
            and result["pip_check_clean"] is True
            and result["imports_ok"] is True
            and result["compileall_metric_status"] == "valid"
            and result["compileall_coverage"] == 1.0
            and result["compileall_foreign_pyc_count"] == 0
        )
        result["success"] = True
        result["complete"] = runtime_complete and not so_missing and not native_missing
        result["reason"] = (
            "Current Python producer receipts and filesystem basis verify setup, "
            "imports, and bytecode compilation"
            if result["complete"]
            else "; ".join(partial_reasons)
        )
        return result

    def _strict_installed_distribution(
        self,
        venv: str,
        project_root: str,
        distribution_name: str,
        *,
        survey_root: Optional[str] = None,
    ) -> Dict[str, any]:
        """Return facts-v8 evidence for exactly one installed distribution.

        A PEP 610 record is accepted only when its local file origin resolves
        to the surveyed Python install root. A different local editable (including a
        provider dependency) is never a fallback.
        """
        site = f"{venv}/lib/python*/site-packages"
        listing = self._execute_command_with_logging(
            f"find {site} -maxdepth 1 "
            f"\\( -name '*.dist-info' -o -name '*.egg-info' \\) "
            f"2>/dev/null",
            f"locating exact {distribution_name} distribution record",
        )
        matches = sorted(
            {
                line.strip()
                for line in (listing.get("output") or "").splitlines()
                if line.strip() and _dist_record_matches(line.strip(), distribution_name)
            }
        )
        if len(matches) != 1:
            detail = "not found" if not matches else f"ambiguous ({len(matches)} records)"
            return {
                "record_ok": False,
                "origin_ok": None,
                "packages": [],
                "reason": (
                    f"Exact installed distribution record for {distribution_name} "
                    f"{detail} — the root project install is not physically verified"
                ),
            }

        record_dir = matches[0]
        direct = self._execute_command_with_logging(
            f"cat {shlex.quote(record_dir)}/direct_url.json 2>/dev/null",
            f"reading {distribution_name} PEP 610 origin",
        )
        direct_text = (direct.get("output") or "").strip()
        if not direct_text:
            return {
                "record_ok": False,
                "origin_ok": False,
                "packages": [],
                "reason": (
                    f"Exact installed distribution {distribution_name} has no PEP 610 "
                    "local-origin record — project ownership is unverified"
                ),
            }
        try:
            payload = json.loads(direct_text)
            parsed = urlparse(str(payload.get("url") or ""))
            if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
                raise ValueError("origin is not a local file URL")
            origin = unquote(parsed.path)
            realpaths = self._execute_command_with_logging(
                "realpath -m -- "
                + " ".join(
                    shlex.quote(path)
                    for path in (
                        "/workspace",
                        origin,
                        project_root,
                        survey_root or project_root,
                    )
                ),
                f"confirming {distribution_name} install origin",
            )
            resolved = [
                line.strip()
                for line in (realpaths.get("output") or "").splitlines()
                if line.strip()
            ]
            origin_ok = (
                realpaths.get("success") is True
                and len(resolved) == 4
                and resolved[0] != "/"
                and resolved[3].startswith(f"{resolved[0].rstrip('/')}/")
                and resolved[1] == resolved[2]
                and (
                    resolved[2] == resolved[3]
                    or resolved[2].startswith(f"{resolved[3].rstrip('/')}/")
                )
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            origin_ok = False
        if not origin_ok:
            return {
                "record_ok": False,
                "origin_ok": False,
                "packages": [],
                "reason": (
                    f"Installed {distribution_name} PEP 610 origin does not "
                    f"resolve to surveyed Python install root {project_root}"
                ),
            }

        top_level = self._execute_command_with_logging(
            f"cat {shlex.quote(record_dir)}/top_level.txt 2>/dev/null",
            f"reading {distribution_name} top_level.txt",
        )
        packages = sorted(
            {
                name
                for line in (top_level.get("output") or "").splitlines()
                if (name := line.strip()) and name not in _NON_PROJECT_TOP_LEVEL
            }
        )
        return {
            "record_ok": True,
            "origin_ok": True,
            "packages": packages,
            "reason": "",
        }

    @staticmethod
    def _manifest_package_imports(package_paths: List[Dict[str, any]]) -> List[str]:
        """Safe import names declared by facts-v8 wheel package mappings."""
        names = []
        for entry in package_paths:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("import_name") or "").strip()
            if not name or not all(part.isidentifier() for part in name.split(".")):
                continue
            if name not in names:
                names.append(name)
        return names

    @staticmethod
    def _manifest_relative_roots(project_root: str, entries: List[str]) -> set[str]:
        """Join trusted relative survey facts without allowing root escape."""
        root = posixpath.normpath(project_root)
        roots: set[str] = set()
        for entry in entries:
            relative = str(entry or "").strip()
            if not relative or relative.startswith("/"):
                continue
            normalized = posixpath.normpath(relative)
            if normalized in ("", ".") or normalized == ".." or normalized.startswith("../"):
                continue
            candidate = posixpath.normpath(posixpath.join(root, normalized))
            if candidate.startswith(f"{root}/"):
                roots.add(candidate)
        return roots

    def _verified_smoke_candidate(
        self,
        project_root: str,
        candidates: List[Dict[str, any]],
    ) -> Optional[str]:
        """First existing, survey-grounded coordinate PythonTool can bound.

        This validator proves only that the coordinate is current and stays
        inside the surveyed root. PythonTool owns scoped collection and the
        1..50 execution bound.
        """
        from sag.tools.internal.python_tool import verified_python_smoke_candidate

        verified = verified_python_smoke_candidate(
            lambda command: self._execute_command_with_logging(
                command,
                "verifying bounded native smoke entry",
            ),
            project_root,
            candidates,
        )
        return verified["absolute_path"] if verified else None

    def _verified_native_artifact(
        self,
        candidates: List[str],
        roots: set[str],
        project_root: str,
    ) -> bool:
        """Require both lexical and realpath containment for native output."""
        normalized_roots = {posixpath.normpath(root) for root in roots}
        for path in candidates:
            candidate = posixpath.normpath(path)
            if not any(
                candidate == root or candidate.startswith(f"{root}/") for root in normalized_roots
            ):
                continue
            ordered_roots = sorted(normalized_roots)
            probe = self._execute_command_with_logging(
                "realpath -m -- "
                + " ".join(
                    shlex.quote(item)
                    for item in ("/workspace", candidate, project_root, *ordered_roots)
                ),
                "confirming native artifact ownership",
            )
            resolved = [
                line.strip() for line in (probe.get("output") or "").splitlines() if line.strip()
            ]
            if not probe.get("success") or len(resolved) != len(ordered_roots) + 3:
                continue
            resolved_workspace, resolved_candidate, resolved_project, *resolved_roots = resolved
            if resolved_workspace == "/" or not resolved_project.startswith(
                f"{resolved_workspace.rstrip('/')}/"
            ):
                continue
            safe_roots = [
                root
                for root in resolved_roots
                if root == resolved_project or root.startswith(f"{resolved_project}/")
            ]
            if any(
                resolved_candidate == root or resolved_candidate.startswith(f"{root}/")
                for root in safe_roots
            ):
                return True
        return False

    def _installed_top_level_packages(self, venv: str, project_dir: str) -> List[str]:
        """Import targets from the PROJECT's OWN installed record when the
        manifest declares no packages — never from third-party dependencies
        sharing the same site-packages.

        Selection ladder: (1) the dist-info whose PEP 610 ``direct_url.json``
        points back into the project dir — the record pip writes for
        ``pip install -e .`` / ``pip install .``; (2) else any dist-info
        with a PEP 610 ``dir_info`` marker — a local-directory install (pip
        records the symlink-resolved realpath, so the exact-tree grep can
        miss; index-installed dependencies carry no direct_url.json at all);
        (3) else the record (``*.dist-info`` / ``*.egg-info``) whose
        distribution name matches the project dir name, PEP 503-normalized,
        version-boundary enforced (_dist_record_matches). A dependency's
        record is never read as project evidence: its broken import must not
        BLOCK, and its working import must not fake imports_ok=True when the
        project's own install failed. Tooling names stay deny-listed as a
        second layer. Empty — an honest, visible imports-rung skip — when no
        record is the project's own."""
        root = project_dir.rstrip("/")
        site = f"{venv}/lib/python*/site-packages"

        def dist_dirs(probe: Dict[str, any]) -> List[str]:
            return [
                line.strip()[: -len("/direct_url.json")]
                for line in (probe.get("output") or "").splitlines()
                if line.strip().endswith("/direct_url.json")
            ]

        direct = self._execute_command_with_logging(
            f"grep -Fls 'file://{root}' {site}/*.dist-info/direct_url.json " f"2>/dev/null",
            "locating the project's own dist-info (direct_url.json)",
        )
        record_dirs = dist_dirs(direct)
        if not record_dirs:
            local = self._execute_command_with_logging(
                f"grep -Fls '\"dir_info\"' {site}/*.dist-info/direct_url.json " f"2>/dev/null",
                "locating local-directory installs (PEP 610 dir_info)",
            )
            record_dirs = dist_dirs(local)
        if not record_dirs:
            listing = self._execute_command_with_logging(
                f"find {site} -maxdepth 1 "
                f"\\( -name '*.dist-info' -o -name '*.egg-info' \\) "
                f"2>/dev/null",
                "listing installed distribution records",
            )
            project_name = root.rsplit("/", 1)[-1]
            record_dirs = [
                line.strip()
                for line in (listing.get("output") or "").splitlines()
                if line.strip() and _dist_record_matches(line.strip(), project_name)
            ]
        packages: List[str] = []
        for record_dir in record_dirs:
            probe = self._execute_command_with_logging(
                f"cat {record_dir}/top_level.txt 2>/dev/null",
                "reading the project's top_level.txt",
            )
            for line in (probe.get("output") or "").splitlines():
                name = line.strip()
                if not name or name in _NON_PROJECT_TOP_LEVEL or name in packages:
                    continue
                packages.append(name)
        return sorted(packages)

    def _python_package_dirs(
        self,
        project_dir: str,
        packages: List[str],
        *,
        package_paths: Optional[List[Dict[str, any]]] = None,
    ) -> List[str]:
        """Package source dirs, src-layout probed before flat layout; the
        project dir is the honest last resort when nothing is declared.

        Facts-v8 wheel package mappings are authoritative when present: only
        their non-escaping source paths enter compileall/coverage.
        """
        root = project_dir.rstrip("/")
        declared_dirs: List[str] = []
        for entry in package_paths or []:
            if not isinstance(entry, dict):
                continue
            relative = str(entry.get("path") or "").strip()
            if not relative or relative.startswith("/"):
                continue
            normalized = posixpath.normpath(relative)
            if normalized in ("", ".") or normalized == ".." or normalized.startswith("../"):
                continue
            candidate = posixpath.normpath(posixpath.join(root, normalized))
            if candidate.startswith(f"{root}/") and candidate not in declared_dirs:
                declared_dirs.append(candidate)
        if declared_dirs:
            return declared_dirs

        dirs: List[str] = []
        for package in packages:
            for candidate in (f"{root}/src/{package}", f"{root}/{package}"):
                probe = self._execute_command_with_logging(
                    f"test -d {candidate}", f"locating package {package}"
                )
                if probe["success"]:
                    dirs.append(candidate)
                    break
        return dirs or [root]

    def _build_status_evidence_refs(
        self, project_dir: str, artifacts_result: Dict[str, any]
    ) -> List[str]:
        """Prefer concrete artifact samples over the project directory reference."""
        refs: List[str] = []
        if artifacts_result.get("class_count", 0) > 0:
            refs.extend((self._check_class_files(project_dir).get("paths") or [])[:5])
        if artifacts_result.get("jar_count", 0) > 0:
            refs.extend((self._check_jar_files(project_dir).get("paths") or [])[:5])
        return list(dict.fromkeys(refs)) or [project_dir]

    def _collect_artifact_samples(
        self, project_dir: str, artifacts_result: Dict[str, any], limit: int = 10
    ) -> List[str]:
        """Real build-output paths (jars first, then classes) for the metrics card.

        Paths are made relative to the project dir for compact display. The
        gradle wrapper jar is never collected: _check_jar_files only matches
        target/build output dirs, so the repo-shipped wrapper jar is excluded.
        """
        samples: List[str] = []
        if artifacts_result.get("jar_count", 0) > 0:
            samples.extend(self._check_jar_files(project_dir).get("paths") or [])
        if artifacts_result.get("class_count", 0) > 0:
            samples.extend(self._check_class_files(project_dir).get("paths") or [])

        prefix = project_dir.rstrip("/") + "/"
        relative = [
            sample[len(prefix) :] if sample.startswith(prefix) else sample
            for sample in dict.fromkeys(samples)
        ]
        return relative[:limit]

    def validate_test_status(self, project_name: str) -> Dict[str, any]:
        """
        Validate test execution status with pass rate calculation.
        Completely separate from build validation.

        Args:
            project_name: Name of the project

        Returns:
            Dict with:
                - has_test_reports: bool
                - total_tests: int
                - passed_tests: int
                - failed_tests: int
                - error_tests: int
                - skipped_tests: int
                - pass_rate: float (0-100)
                - test_exclusions: List[str] (detected excluded tests)
                - status: str (SUCCESS/WARNING/PARTIAL/FAILED)
                - reason: str (explanation of the status)
        """
        logger.info(f"Starting test validation for project: {project_name}")

        project_dir = f"{self.project_path}/{project_name}" if project_name else self.project_path

        # Parse test reports with enhanced metrics and catalog integration
        test_metrics = self.parse_test_reports_with_catalog(project_dir)

        # Calculate pass rate
        pass_rate = self.calculate_test_pass_rate(test_metrics)
        execution_summary = self._test_execution_receipt_summary(project_dir)

        # pytest collection nodes are NOT executed tests (Plan 4 Task 2). They
        # travel with the verdict so the report can quote the real root cause
        # instead of inventing "N tests errored".
        collection_errors = test_metrics.get("collection_errors", 0) or 0
        collection_errors_skipped = test_metrics.get("collection_errors_skipped", 0) or 0
        collection_error_summary = test_metrics.get("collection_error_summary")

        failed_count = test_metrics.get("failed_tests", 0) + test_metrics.get("error_tests", 0)
        # ONE census producer decides the denominator (#39 §2.1). Python
        # projects: python_tool's pytest --collect-only rows are ground truth
        # from the actual runner and take PRIORITY over any static heuristic
        # the runner metrics may carry (live 2026-07-10 click run: a static
        # scan that swept the .venv reported 32927 while pytest collected
        # 1927), which is also what keeps a Java @Test scan out of a Python
        # denominator. Otherwise the module breakdown is the auditable
        # producer and its sum wins: polaris sealed 1,347 beside a module list
        # that explained 593, and the OR-chain that used to stand here picked
        # whichever source answered first with no provenance either way.
        census = produce_census(
            by_module=test_metrics.get("catalog_by_module"),
            module_total=test_metrics.get("catalog_module_total"),
            bare_total=(
                test_metrics.get("discovered")
                or test_metrics.get("discovered_tests")
                or test_metrics.get("static_test_count")
                or test_metrics.get("catalog_test_count")
            ),
            collected=self._python_collected_count(project_name),
        )
        discovered = census.discovered

        # One decision, three fields (spec §2.3). The pass rate is reported as a
        # fact below; it decides nothing here.
        decision = decide_test_evidence(
            valid=bool(test_metrics.get("valid", False)),
            executed=test_metrics.get("total_tests", 0) or 0,
            discovered=discovered,
            passed=test_metrics.get("passed_tests", 0) or 0,
            failed=test_metrics.get("failed_tests", 0) or 0,
            errors=test_metrics.get("error_tests", 0) or 0,
            skipped=test_metrics.get("skipped_tests", 0) or 0,
        )
        status, evidence_status, reason = decision

        # A dead collection is not "no tests executed" with no cause: it names
        # the cause. Same direction as the decision it refines — still FAILED,
        # still a deficiency sentence.
        if collection_errors and not test_metrics.get("total_tests", 0):
            status = "FAILED"
            evidence_status = "blocked"
            reason = f"Test collection failed for {collection_errors} files — 0 tests executed"
            if collection_error_summary:
                reason = f"{reason}: {collection_error_summary}"

        if execution_summary.get("state") == "partial":
            status = "PARTIAL"
            evidence_status = "partial"
            reason = str(execution_summary.get("reason") or "test execution was interrupted")
        elif execution_summary.get("state") == "failed" and not test_metrics.get("total_tests", 0):
            status = "FAILED"
            evidence_status = "blocked"
            reason = str(
                execution_summary.get("reason")
                or "test execution was interrupted before producing results"
            )

        has_test_count_evidence = test_metrics.get("valid", False) or any(
            key in test_metrics and test_metrics.get(key) is not None
            for key in (
                "discovered",
                "discovered_tests",
                "static_test_count",
                "total_tests",
                "passed_tests",
                "failed_tests",
                "error_tests",
                "skipped_tests",
            )
        )
        test_stats = None
        if has_test_count_evidence and test_metrics.get("valid", False):
            test_stats = {
                "discovered": discovered,
                # The census travels WITH the number it produced, or the
                # denominator arrives downstream as a bare integer again.
                **_census_facts(census),
                "executed": test_metrics.get("total_tests", 0),
                "passed": test_metrics.get("passed_tests", 0),
                "failed": failed_count,
                "skipped": test_metrics.get("skipped_tests", 0),
                "flaky_count": test_metrics.get("flaky_count", 0),
                "pass_rate": round(pass_rate, 1),
                "collection_errors": collection_errors,
                **(
                    {"driven_modules": list(test_metrics["driven_modules"])}
                    if isinstance(test_metrics.get("driven_modules"), list)
                    else {}
                ),
                **(
                    {"test_modules": list(test_metrics["test_modules"])}
                    if isinstance(test_metrics.get("test_modules"), list)
                    else {}
                ),
            }
        report_files = test_metrics.get("report_files", [])
        conflicts = list(census.conflicts)
        if test_metrics.get("failed_tests", 0):
            conflicts.append("test_failures_detected")
        if test_metrics.get("error_tests", 0):
            conflicts.append("test_errors_detected")
        # Collection nodes no longer inflate error_tests, so the verdict cap
        # they used to (accidentally) carry needs its own honest signal.
        if collection_errors:
            conflicts.append("test_collection_failed")
        if test_metrics.get("parsing_errors", []):
            conflicts.append("test_report_parse_error")
        if test_metrics.get("metrics_conflicts", []):
            conflicts.append("metrics_conflict")

        # Receipt-scoped evidence (Plan 5 Task B2). Superseded reports are a
        # visible conflict — named, pathed and counted, and adjudicated rather
        # than capping (spec 2026-08-14 amendment item 7). An unreadable receipt
        # is a different thing: an evidence-closure failure that no pass rate
        # may paper over.
        if test_metrics.get("stale_test_reports"):
            conflicts.append(STALE_CONFLICT)
        receipt_error = test_metrics.get("receipt_error")
        if receipt_error:
            conflicts.append("test_receipt_unreadable")
            evidence_status = "conflict"
            status = "FAILED"
            reason = f"Invocation-receipt evidence is unreadable: {receipt_error}"

        result = {
            "has_test_reports": test_metrics.get("valid", False),
            "total_tests": test_metrics.get("total_tests", 0),
            "passed_tests": test_metrics.get("passed_tests", 0),
            "failed_tests": test_metrics.get("failed_tests", 0),
            "error_tests": test_metrics.get("error_tests", 0),
            "skipped_tests": test_metrics.get("skipped_tests", 0),
            "pass_rate": pass_rate,
            "failing_test_names": test_metrics.get("failing_test_names", []),
            "test_exclusions": test_metrics.get("test_exclusions", []),
            "modules_without_tests": test_metrics.get("modules_without_tests", []),
            "status": status,
            "reason": reason,
            "report_files": report_files,
            "report_file_count": test_metrics.get("report_file_count", len(report_files or [])),
            "raw_total_tests": test_metrics.get("raw_total_tests"),
            "raw_passed_tests": test_metrics.get("raw_passed_tests"),
            "raw_failed_tests": test_metrics.get("raw_failed_tests"),
            "raw_error_tests": test_metrics.get("raw_error_tests"),
            "raw_skipped_tests": test_metrics.get("raw_skipped_tests"),
            "unique_tests": test_metrics.get("unique_tests"),
            "unique_passed_tests": test_metrics.get("unique_passed_tests"),
            "unique_failed_tests": test_metrics.get("unique_failed_tests"),
            "unique_error_tests": test_metrics.get("unique_error_tests"),
            "unique_skipped_tests": test_metrics.get("unique_skipped_tests"),
            "flaky_count": test_metrics.get("flaky_count", 0),
            "retried_count": test_metrics.get("retried_count", 0),
            "collection_errors": collection_errors,
            "collection_errors_skipped": collection_errors_skipped,
            "collection_error_summary": collection_error_summary,
            "test_histories": test_metrics.get("test_histories", []),
            "metrics_conflicts": test_metrics.get("metrics_conflicts", []),
            "parsing_errors": test_metrics.get("parsing_errors", []),
            "evidence_status": evidence_status,
            "test_stats": test_stats,
            # The discovered-suite denominator (analyzer count or the pytest
            # collect-only fallback) — consumers like the report snapshot's
            # execution-coverage gate read this even when test_stats is None.
            "static_test_count": discovered,
            # ... and what that denominator covers, for the same reason: a
            # consumer reading the number without the basis cannot tell a
            # complete survey from a floor.
            **_census_facts(census),
            "conflicts": list(dict.fromkeys(conflicts)),
            "evidence_refs": list(report_files) or [project_dir],
        }
        if execution_summary.get("state") != "unknown":
            result["test_execution_state"] = execution_summary["state"]
            result["test_execution_reason"] = execution_summary.get("reason")
            result["test_execution_receipt_ids"] = list(execution_summary.get("receipt_ids") or ())
            interrupted_ids = list(execution_summary.get("interrupted_receipt_ids") or ())
            if interrupted_ids:
                result["test_interrupted_receipt_ids"] = interrupted_ids
        # Absent facts = absent keys: a receipt-free run carries none of these,
        # so its status dict (and everything projected from it) is unchanged.
        for key in (
            "receipt_scoped",
            "auxiliary_test_stats",
            "auxiliary_report_files",
            "stale_test_reports",
            "stale_test_stats",
            "unmeasured_test_reports",
            "unmeasured_test_stats",
            "receipt_error",
            "receipt_error_files",
        ):
            value = test_metrics.get(key)
            if value:
                result[key] = value

        logger.info(f"Test validation complete: {status} - {reason}")
        if result["test_exclusions"]:
            logger.warning(f"Detected test exclusions: {', '.join(result['test_exclusions'])}")
        if result["modules_without_tests"]:
            untested_count = len(result["modules_without_tests"])
            logger.warning(
                f"⚠️ MISSING TEST REPORTS: {untested_count} modules have no test results: {', '.join(result['modules_without_tests'])}"
            )
            logger.warning(
                f"📊 Found {result['total_tests']} tests executed, but some modules were skipped"
            )
            logger.info(
                "Observed constraint: module test coverage is incomplete; "
                "the validator does not select a repair command"
            )

        return result

    def validate_project_analysis_status(self, project_name: str = None) -> Dict[str, any]:
        """
        Validate whether project analysis has been performed.

        Checks for presence of static test count and other analysis markers
        in the trunk context to determine if project_analyzer was run.

        Args:
            project_name: Optional project name for context

        Returns:
            Dictionary with:
                - analyzed: Boolean indicating if analysis was performed
                - has_static_test_count: Boolean for test count presence
                - static_test_count: The actual count if available
                - analysis_status_code: Typed absence/incompleteness code
                - analysis_status_facts: Bounded physical status facts
        """
        result = {
            "analyzed": False,
            "has_static_test_count": False,
            "static_test_count": None,
            "analysis_status_code": None,
            "analysis_status_facts": {},
            "trunk_context_found": False,
        }

        try:
            # Try to find and load trunk context
            trunk_file_cmd = (
                f"ls {self.project_path}/.setup_agent/contexts/trunk_*.json 2>/dev/null | head -1"
            )
            trunk_file_result = self._execute_command_with_logging(
                trunk_file_cmd, "finding trunk context file"
            )

            if not trunk_file_result["success"] or not trunk_file_result.get("output"):
                logger.warning(
                    "No trunk context file found - project analysis likely not performed"
                )
                result["analysis_status_code"] = "analysis_trunk_missing"
                result["analysis_status_facts"] = {"trunk_context_found": False}
                return self._apply_python_collected_denominator(result, project_name)

            trunk_file = trunk_file_result["output"].strip()
            result["trunk_context_found"] = True

            # Load and parse trunk context
            load_cmd = f"cat {trunk_file}"
            load_result = self._execute_command_with_logging(load_cmd, "loading trunk context")

            if load_result["success"] and load_result.get("output"):
                import json

                try:
                    trunk_data = json.loads(load_result["output"])
                    env_summary = trunk_data.get("environment_summary", {})

                    # Check for static test count. The trunk carries a BARE
                    # total beside the module breakdown that should explain it;
                    # both go through the one census producer here, so this
                    # read can never be the second source that contradicts the
                    # sealed denominator (#39 §2.1).
                    census = census_from_catalog_summary(
                        env_summary.get("test_catalog_summary"),
                        bare_total=env_summary.get("static_test_count"),
                    )
                    static_test_count = census.discovered
                    if static_test_count is not None:
                        result["has_static_test_count"] = True
                        result["static_test_count"] = static_test_count
                        result["analyzed"] = True
                        result.update(_census_facts(census))
                        logger.info(
                            f"✅ Project analysis found: {static_test_count} static tests detected"
                        )
                    else:
                        logger.warning("⚠️ Trunk context exists but no static_test_count found")
                        result["analysis_status_code"] = "analysis_static_count_missing"
                        result["analysis_status_facts"] = {
                            "trunk_context_found": True,
                            "static_test_count_present": False,
                        }

                    # Check for other analysis markers. dim (c) deleted
                    # (Category-3 analyzer diet): the analyzer no longer writes
                    # project_brief_ref/fingerprint, so the survey facts
                    # (build system / recommendation) are the readiness markers.
                    if any(
                        (
                            env_summary.get("project_type"),
                            env_summary.get("build_system"),
                            env_summary.get("build_recommendation"),
                            env_summary.get("survey"),
                        )
                    ):
                        result["analyzed"] = True  # At least partial analysis was done

                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse trunk context JSON: {e}")

        except Exception as e:
            logger.error(f"Error validating project analysis status: {e}")

        if not result["analyzed"] and not result["analysis_status_code"]:
            result["analysis_status_code"] = "analysis_facts_missing"
            result["analysis_status_facts"] = {
                "trunk_context_found": result["trunk_context_found"],
            }

        return self._apply_python_collected_denominator(result, project_name)

    def _apply_python_collected_denominator(
        self, result: Dict[str, any], project_name: Optional[str]
    ) -> Dict[str, any]:
        """Use the current published Python test receipt as denominator.

        The historical ``pytest_collected.json`` mirror is container-writable
        and cannot decide a live verdict. A complete typed testcase envelope
        on a current run/checkout/root-bound InvocationReceipt may replace the
        static scan; otherwise the denominator stays unknown/falls back.
        """
        collected = self._python_collected_count(project_name)
        if collected is None:
            return result
        if result.get("has_static_test_count") and result.get("static_test_count") != collected:
            # Preserve the static-scan number as evidence; it no longer feeds
            # the execution-coverage gate.
            result["static_test_count_static_scan"] = result["static_test_count"]
            logger.info(
                f"✅ python denominator priority: {collected} tests from pytest "
                f"receipt rows override the static scan count "
                f"{result['static_test_count']}"
            )
        else:
            logger.info(
                f"✅ static_test_count: {collected} tests from "
                f"a host-published Python test receipt"
            )
        result["has_static_test_count"] = True
        result["static_test_count"] = collected
        result["static_test_count_source"] = "published_testcase_receipt"
        if result.get("analyzed") and result.get("analysis_status_code") == (
            "analysis_static_count_missing"
        ):
            result["analysis_status_code"] = None
            result["analysis_status_facts"] = {}
        return result

    def _python_collected_count(self, project_name: Optional[str]) -> Optional[int]:
        """Conservative denominator from current published Python test rows.

        Multiple current attempts contribute their maximum complete row set;
        a later filtered retry cannot shrink an earlier full-suite denominator.
        Missing, malformed, unpublished, stale, or scope-unbound receipts are
        unknown. The raw ``pytest_collected.json`` mirror is never read here.
        """
        try:
            from sag.agent.attempt_policy import (
                current_run_durable_receipt,
                resolve_current_build_receipt_scope,
            )
            from sag.agent.invocation_receipts import (
                active_receipt_run_id,
                nearest_domain_fact_epoch,
                nearest_domain_root,
                survey_pins,
            )
            from sag.tools.internal.build_preflight import read_live_build_requirements

            project_dir = (
                f"{self.project_path}/{project_name}" if project_name else self.project_path
            )
            if self._detect_build_system(project_dir) != "python":
                return None
            manifest_read = read_live_build_requirements(self.docker_orchestrator)
            if (
                not manifest_read.complete
                or manifest_read.conflict is not None
                or manifest_read.payload is None
            ):
                return None
            manifest = dict(manifest_read.payload)
            records = self._read_live_invocation_receipts()
            if not records:
                return None
            run_id = self.receipt_run_id or active_receipt_run_id()
            scope = resolve_current_build_receipt_scope(
                self.docker_orchestrator,
                run_id=run_id,
                workspace_root=self.project_path,
                project_root=project_dir,
            )
            if not scope.available:
                return None
            pins = survey_pins(manifest)
            domain_id = nearest_domain_root(manifest, project_dir)
            fact_epoch = nearest_domain_fact_epoch(manifest, project_dir)
            counts: List[int] = []
            for receipt in records:
                receipt_id = str(receipt.get("receipt_id") or "")
                if (
                    receipt.get("tool") != "python"
                    or receipt.get("effective_action") != "test"
                    or not _dispatch_terminated(receipt)
                    or not current_run_durable_receipt(
                        receipt,
                        receipt_id=receipt_id,
                        run_id=run_id,
                        target_sha=scope.target_sha or "",
                        project_root=scope.project_root or project_dir,
                    )
                    or not self._python_producer_contract_valid(
                        receipt,
                        operation="test",
                        project_root=project_dir,
                        target_sha=scope.target_sha or "",
                        survey_fingerprint=pins.get("survey_fingerprint"),
                        config_fingerprint=pins.get("config_fingerprint"),
                        document_map_fingerprint=pins.get("document_map_fingerprint"),
                        domain_id=domain_id,
                        fact_epoch=fact_epoch,
                    )
                ):
                    continue
                envelope = receipt.get("testcase_execution_rows")
                if not isinstance(envelope, Mapping) or envelope.get("status") != "complete":
                    continue
                rows = envelope.get("rows")
                if not isinstance(rows, list) or not rows:
                    continue
                counts.append(len(rows))
            return max(counts) if counts else None
        except Exception as exc:
            logger.debug(f"published Python denominator unavailable: {exc}")
            return None

    def parse_test_reports_with_catalog(
        self, project_dir: str, test_catalog: Optional[TestCaseCatalog] = None
    ) -> Dict[str, any]:
        """
        Enhanced test report parsing with test catalog integration.

        Automatically builds a test catalog if not provided and uses it for
        comparison to detect unexecuted tests.

        Args:
            project_dir: Project directory path
            test_catalog: Optional pre-built catalog from static analysis

        Returns:
            Dictionary with test metrics including unexecuted test detection
        """
        # Build catalog if not provided and project is in workspace
        if test_catalog is None and project_dir.startswith("/workspace"):
            logger.debug("Building test catalog for enhanced analysis...")
            test_catalog = build_java_test_catalog(project_dir, self.docker_orchestrator)
            logger.info(f"📊 Built catalog with {test_catalog.count()} test methods")

        # Parse with catalog for enhanced analysis
        result = self.parse_test_reports(project_dir, test_catalog)

        # Add catalog metadata to result if available
        if test_catalog:
            by_module = test_catalog.to_dict()["by_module"]
            result["catalog_test_count"] = test_catalog.count()
            result["catalog_by_module"] = by_module
            # How many modules the catalog HOLDS, beside how many it names
            # here. They differ only where a bounded projection dropped the
            # tail, and that difference is exactly what makes the denominator
            # a floor rather than a survey (#39 §2.2).
            result["catalog_module_total"] = len(by_module)

        return result

    def get_unexecuted_tests_log(self, project_dir: str) -> Optional[Dict[str, any]]:
        """
        Retrieve the unexecuted tests log if it exists.

        Args:
            project_dir: Project directory path (for reference, log is in /workspace/.setup_agent/)

        Returns:
            Dictionary with unexecuted test data or None if not found
        """
        try:
            log_file_path = "/workspace/.setup_agent/unexecuted_tests.json"

            if self.docker_orchestrator:
                # Check if file exists
                check_cmd = f"test -f {log_file_path} && echo 'exists'"
                check_result = self.docker_orchestrator.execute_command(check_cmd)

                if check_result.get("success") and "exists" in check_result.get("output", ""):
                    # Read the file
                    read_cmd = f"cat {log_file_path}"
                    read_result = self.docker_orchestrator.execute_command(read_cmd)

                    if read_result.get("success"):
                        try:
                            return json.loads(read_result.get("output", "{}"))
                        except json.JSONDecodeError as e:
                            logger.error(f"Failed to parse unexecuted tests log: {e}")
                            return None
            else:
                # Direct file read for testing
                if os.path.exists(log_file_path):
                    with open(log_file_path, "r", encoding="utf-8") as f:
                        return json.load(f)

            return None

        except Exception as e:
            logger.error(f"Error retrieving unexecuted tests log: {e}")
            return None

    def parse_test_reports_with_metrics(self, project_dir: str) -> Dict[str, any]:
        """
        Enhanced test report parsing with pass rate calculations and exclusion detection.
        Extends the existing parse_test_reports with additional metrics.

        Args:
            project_dir: Project directory path

        Returns:
            Dictionary with enhanced test statistics including:
                - All fields from parse_test_reports
                - test_exclusions: List of detected excluded tests
                - modules_without_tests: List of modules that lack test reports
        """
        # Start with the base test report parsing
        base_result = self.parse_test_reports(project_dir)

        # Add enhanced metrics
        base_result["test_exclusions"] = self._detect_test_exclusions(project_dir)

        # We don't calculate coverage - that's about test quality, not our concern
        # SAG only cares that tests were executed, not how comprehensive they are
        # The actual test count from reports is the only truth we need

        return base_result

    def calculate_test_pass_rate(self, test_metrics: Dict[str, any]) -> float:
        """
        Calculate test pass rate from test statistics.

        Args:
            test_metrics: Dictionary containing test statistics

        Returns:
            Pass rate as percentage (0-100)
        """
        total = test_metrics.get("total_tests", 0) or test_metrics.get("raw_total_tests", 0)
        if total == 0:
            return 0.0

        passed = test_metrics.get("passed_tests", 0)
        if passed == 0 and test_metrics.get("unique_passed_tests", 0) > 0:
            passed = test_metrics.get("unique_passed_tests", 0)
        if passed == 0 and test_metrics.get("raw_passed_tests", 0) > 0:
            passed = test_metrics.get("raw_passed_tests", 0)
        return (passed / total) * 100

    def _detect_test_exclusions(self, project_dir: str) -> List[str]:
        """
        Detect test exclusion patterns in build configuration or recent commands.

        Returns:
            List of detected test exclusion patterns
        """
        exclusions = []

        try:
            # Maven: scan all pom.xml files but only extract excludes inside surefire/failsafe plugin blocks
            poms_cmd = f"find {project_dir} -type f -name 'pom.xml' 2>/dev/null"
            poms_result = self._execute_command_with_logging(
                poms_cmd, "discovering Maven POMs for exclusions"
            )
            pom_files = (
                [p.strip() for p in (poms_result.get("output") or "").split("\n") if p.strip()]
                if poms_result["success"]
                else []
            )

            plugin_pattern = re.compile(
                r"<plugin>[\s\S]*?<artifactId>\s*maven-(surefire|failsafe)-plugin\s*</artifactId>[\s\S]*?</plugin>",
                re.IGNORECASE,
            )
            exclude_tag_pattern = re.compile(r"<exclude>\s*([^<]+?)\s*</exclude>", re.IGNORECASE)
            skip_flag_pattern = re.compile(r"<skipTests>\s*true\s*</skipTests>", re.IGNORECASE)

            for pom in pom_files:
                cat_res = self._execute_command_with_logging(
                    f"cat '{pom}' 2>/dev/null || true", f"reading {pom}"
                )
                if not cat_res["success"] or not cat_res.get("output"):
                    continue
                content = cat_res["output"]
                for plugin_block in plugin_pattern.findall(content) or []:
                    # The regex returns only the group; re-find full blocks to extract excludes
                    for block_match in re.finditer(
                        r"<plugin>[\s\S]*?<artifactId>\s*maven-(?:surefire|failsafe)-plugin\s*</artifactId>[\s\S]*?</plugin>",
                        content,
                        re.IGNORECASE,
                    ):
                        block = block_match.group(0)
                        exclusions.extend(exclude_tag_pattern.findall(block))
                        if skip_flag_pattern.search(block):
                            exclusions.append("ALL_TESTS_SKIPPED")

            # Gradle: inspect test{} blocks only
            gradle_cmd = f"find {project_dir} -type f \\(-name 'build.gradle' -o -name 'build.gradle.kts'\\) 2>/dev/null"
            gradle_files_res = self._execute_command_with_logging(
                gradle_cmd, "discovering Gradle build files for exclusions"
            )
            gradle_files = (
                [g.strip() for g in (gradle_files_res.get("output") or "").split("\n") if g.strip()]
                if gradle_files_res["success"]
                else []
            )

            for gf in gradle_files:
                gcat = self._execute_command_with_logging(
                    f"cat '{gf}' 2>/dev/null || true", f"reading {gf}"
                )
                if not gcat["success"] or not gcat.get("output"):
                    continue
                gcontent = gcat["output"]
                # Capture test { ... } blocks
                for test_block_match in re.finditer(
                    r"test\s*\{([\s\S]*?)\}", gcontent, re.IGNORECASE
                ):
                    block = test_block_match.group(1)
                    # Direct exclude 'exclude "pattern"' or 'exclude 'pattern''
                    exclusions.extend(re.findall(r"exclude\s*['\"]([^'\"]+)['\"]", block))
                    # excludeTestsMatching inside filter {}
                    for filter_block in re.findall(
                        r"filter\s*\{([\s\S]*?)\}", block, re.IGNORECASE
                    ):
                        exclusions.extend(
                            re.findall(r"excludeTestsMatching\s*['\"]([^'\"]+)['\"]", filter_block)
                        )
                # useJUnitPlatform { excludeTags 'slow' }
                for ujp_block in re.finditer(
                    r"useJUnitPlatform\s*\{([\s\S]*?)\}", gcontent, re.IGNORECASE
                ):
                    tags = re.findall(r"excludeTags\s*['\"]([^'\"]+)['\"]", ujp_block.group(1))
                    exclusions.extend([f"EXCLUDE_TAG:{t}" for t in tags])

            # Check for skip flags in recent commands (from command history if available)
            history_cmd = f"grep -E 'DskipTests|-x test|--exclude-task test|Dtest=' {project_dir}/.setup_agent/command_history.txt 2>/dev/null || true"
            history_result = self._execute_command_with_logging(
                history_cmd, "checking command history for test skips"
            )
            if history_result["success"] and history_result.get("output"):
                # P0-C (Plan 5 Stage D): -DskipTests / -x test on a PACKAGING
                # command is the install/package contract (packaging never runs
                # tests), not a test exclusion — only test-owning commands may
                # count as excluding tests.
                hist = "\n".join(
                    line
                    for line in history_result["output"].splitlines()
                    if not re.search(r"\b(install|package|assemble|publishToMavenLocal)\b", line)
                )
                if "-DskipTests" in hist or "skipTests=true" in hist:
                    exclusions.append("ALL_TESTS_SKIPPED")
                if "-x test" in hist or "--exclude-task test" in hist:
                    exclusions.append("GRADLE_TESTS_EXCLUDED")
                # Extract -Dtest=!Pattern and -Dtest=Pattern
                exclusions.extend(re.findall(r"-Dtest=!([^\s]+)", hist))
                exclusions.extend(
                    [f"INCLUDE_TEST:{m}" for m in re.findall(r"-Dtest=([^!\s][^\s]*)", hist)]
                )

        except Exception as e:
            logger.warning(f"Failed to detect test exclusions: {e}")

        # Remove duplicates and return
        return list(set(exclusions))

    def _detect_build_system(self, project_dir: str) -> str:
        """
        Detect the build system used by the project.

        Returns:
            'maven', 'gradle', 'npm', 'python', or 'unknown'
        """
        # Check for build files
        checks = [
            ("pom.xml", "maven"),
            ("build.gradle", "gradle"),
            ("build.gradle.kts", "gradle"),
            ("package.json", "npm"),
            ("requirements.txt", "python"),
            ("setup.py", "python"),
            ("pyproject.toml", "python"),
        ]

        for filename, build_system in checks:
            cmd = f"test -f {project_dir}/{filename}"
            result = self._execute_command_with_logging(cmd, f"checking for {filename}")
            if result["success"]:
                return build_system

        return "unknown"

    def detect_java_build_systems(self, project_dir: str) -> List[str]:
        """Every Java build system with marker files in the module-scan range.

        _detect_build_system answers "what is THE build system" (first ROOT
        marker wins), which is blind to mixed layouts: a Maven-rooted repo
        whose tests live in a Gradle subtree (live bigtop:
        bigtop-data-generators) reports only 'maven', so the Gradle test
        cluster's modules never enter the module scan. This probe reports each
        of maven/gradle whose markers exist at the root OR within the same
        submodule depth range scan_modules enumerates (mindepth 2..maxdepth 3).
        """
        if not self.docker_orchestrator:
            return []
        markers = [
            ("maven", ["pom.xml"]),
            ("gradle", ["build.gradle", "build.gradle.kts"]),
        ]
        present: List[str] = []
        for system, names in markers:
            root_test = " || ".join(f"test -f {project_dir}/{n}" for n in names)
            name_expr = " -o ".join(f"-name '{n}'" for n in names)
            if len(names) > 1:
                name_expr = f"\\( {name_expr} \\)"
            cmd = (
                f"({root_test}) && echo EXISTS || "
                f"find {project_dir} -mindepth 2 -maxdepth 3 {name_expr} -type f "
                f"2>/dev/null | head -1"
            )
            result = self._execute_command_with_logging(cmd, f"probing {system} markers")
            if result.get("success") and (result.get("output") or "").strip():
                present.append(system)
        return present

    def _check_build_artifacts_complete(self, project_dir: str) -> Dict[str, any]:
        """
        Complete check of build artifacts without head limits.

        Returns:
            Dict with artifact existence and counts
        """
        cache_key = self._get_cache_key("artifacts_complete", project_dir)
        cached = self._get_cached_result(cache_key)
        if cached:
            return cached

        result = {"exist": False, "count": 0, "jar_count": 0, "class_count": 0, "details": {}}

        # Count JAR files (complete scan, no head limit). Exclude the Gradle
        # wrapper/tooling jar: it ships with the repo and is NOT a build output,
        # so it must never count as evidence that the project compiled.
        jar_cmd = (
            f"find {project_dir} -name '*.jar' -type f "
            f"-not -path '*/gradle/wrapper/*' 2>/dev/null | wc -l"
        )
        jar_result = self._execute_command_with_logging(jar_cmd, "counting JAR files")
        if jar_result["success"]:
            result["jar_count"] = int(jar_result["output"].strip() or 0)

        # Count class files (complete scan)
        class_cmd = f"find {project_dir} -name '*.class' -type f 2>/dev/null | wc -l"
        class_result = self._execute_command_with_logging(class_cmd, "counting class files")
        if class_result["success"]:
            result["class_count"] = int(class_result["output"].strip() or 0)

        # Check for Node modules only if a Node.js project is detected
        package_json_cmd = (
            f"find {project_dir} -maxdepth 2 -name 'package.json' -type f 2>/dev/null | head -1"
        )
        package_json_result = self._execute_command_with_logging(
            package_json_cmd, "discovering package.json"
        )
        if package_json_result["success"] and package_json_result["output"].strip():
            node_cmd = f"test -d {project_dir}/node_modules && find {project_dir}/node_modules -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l"
            node_result = self._execute_command_with_logging(node_cmd, "checking Node modules")
            if node_result["success"]:
                result["details"]["node_modules"] = int(node_result["output"].strip() or 0)

        # Determine if artifacts exist
        result["count"] = (
            result["jar_count"] + result["class_count"] + result["details"].get("node_modules", 0)
        )
        result["exist"] = result["count"] > 0

        self._cache_result(cache_key, result)
        return result

    def scan_modules(self, project_dir: str, build_system: str) -> List[Dict[str, any]]:
        """Enumerate submodules and scan each for artifacts + test report dirs.

        Backbone of the per-module metrics: physical evidence of which modules
        exist and what each produced. Returns a flat list of module records.
        Single-module projects return one record with path '.'.
        """
        if not self.docker_orchestrator:
            return []

        if build_system == "gradle":
            find_cmd = (
                f"find {project_dir} -mindepth 2 -maxdepth 3 "
                f"\\( -name 'build.gradle' -o -name 'build.gradle.kts' \\) 2>/dev/null"
            )
            classes_glob = "build/classes"
            jars_glob = "build/libs"
            report_subdirs = ["build/test-results/test", "build/test-results"]
            sep = ":"
        else:
            find_cmd = (
                f"find {project_dir} -mindepth 2 -maxdepth 3 -name 'pom.xml' -type f 2>/dev/null"
            )
            classes_glob = "target/classes"
            jars_glob = "target"
            report_subdirs = ["target/surefire-reports", "target/failsafe-reports"]
            sep = ":"

        found = self._execute_command_with_logging(find_cmd, "enumerating submodules")
        lines = [l for l in (found.get("output") or "").splitlines() if l.strip()]
        module_dirs = sorted({l.rsplit("/", 1)[0] for l in lines})

        # Gradle declares its subprojects CENTRALLY. Kafka and samza list every
        # one in settings.gradle beside a single root build.gradle, so the
        # per-directory walk above sees no submodule at all: D2 kafka scanned a
        # denominator of 2 (root + two stray quickstart poms) on a build with
        # dozens of subprojects, and the 11,421 classes sitting in their
        # unenumerated `build/classes` were invisible to every per-module probe.
        # A module the settings file declares is a module.
        #
        # Maven needs no counterpart: `<module>` names a directory that MUST
        # contain that module's own pom.xml, so the pom walk above already
        # enumerates every declared module. There is no Maven twin of this gap.
        declared_count = 0
        if build_system == "gradle":
            declared = self._declared_gradle_subprojects(project_dir)
            declared_count = len(declared)
            if declared:
                # One probe for all of them, and only directories that EXIST
                # join the denominator: a declaration the disk does not back
                # (a relocated `projectDir`) would otherwise manufacture a
                # permanent shortfall no build could close.
                candidates = " ".join(shlex.quote(f"{project_dir}/{rel}") for rel in declared)
                probe = self._execute_command_with_logging(
                    f'for d in {candidates}; do test -d "$d" && echo "$d"; done',
                    "locating declared gradle subprojects",
                )
                on_disk = [l.strip() for l in (probe.get("output") or "").splitlines() if l.strip()]
                module_dirs = sorted(set(module_dirs) | set(on_disk))

        # Always scan the root module too. The submodule find runs at mindepth 2,
        # so the depth-1 root pom is excluded — a root that compiled its own
        # sources (e.g. commons-chain's 33 classes) would otherwise be invisible
        # in the breakdown while unrelated example submodules showed as "0 built".
        # Single-module projects (no submodules found) collapse to just the root.
        if project_dir not in module_dirs:
            module_dirs = [project_dir] + module_dirs

        modules: List[Dict[str, any]] = []
        for module_dir in module_dirs:
            rel = module_dir[len(project_dir) :].strip("/") or "."
            name = "." if rel == "." else rel.replace("/", sep)

            class_cmd = (
                f"find '{module_dir}/{classes_glob}' -name '*.class' -type f 2>/dev/null | wc -l"
            )
            cc = self._execute_command_with_logging(class_cmd, f"counting classes in {rel}")
            # None (not 0) when the count command fails: "couldn't measure" must
            # not masquerade as "zero classes" (which would also wrongly suppress
            # the artifact-based build inference downstream).
            class_count = int((cc.get("output") or "0").strip() or 0) if cc.get("success") else None

            jar_cmd = (
                f"find '{module_dir}/{jars_glob}' -name '*.jar' -type f "
                f"-not -path '*/gradle/wrapper/*' 2>/dev/null | wc -l"
            )
            jc = self._execute_command_with_logging(jar_cmd, f"counting jars in {rel}")
            jar_count = int((jc.get("output") or "0").strip() or 0) if jc.get("success") else None

            report_dirs: List[str] = []
            for sub in report_subdirs:
                rd = f"{module_dir}/{sub}"
                chk = self._execute_command_with_logging(
                    f"test -d {rd} && echo EXISTS", f"checking reports {rel}"
                )
                if "EXISTS" in (chk.get("output") or ""):
                    report_dirs.append(rd)

            # Test-bearing probe: does this module declare test sources? Feeds
            # modules_test_bearing so the report can tell "tests ran in a strict
            # subset of the test-bearing modules" (reactor_scope_narrowed) apart
            # from "these modules simply have no tests" (spec §4).
            tst = self._execute_command_with_logging(
                f"test -d {module_dir}/src/test && echo EXISTS",
                f"checking test sources {rel}",
            )
            has_test_sources = "EXISTS" in (tst.get("output") or "")

            record = {
                "path": rel,
                "name": name,
                "class_count": class_count,
                "jar_count": jar_count,
                "report_dirs": report_dirs,
                "has_test_sources": has_test_sources,
            }
            if declared_count:
                # What the build DECLARED, carried beside what the scan found so
                # a reader downstream can tell a small project from a blind scan
                # (module_coverage lifts it to the summary). Stamped on every
                # row rather than the root's: the mixed-layout merge keeps the
                # richer record per path, and the root's may lose.
                record["declared_modules"] = declared_count

            # Aggregator-shell detection (root record only, in a MULTI-module
            # scan). Live httpcomponents-client: the reactor root is a Maven
            # packaging=pom aggregator with zero sources — it produces nothing
            # by design, yet it was counted as an unbuilt denominator entry,
            # capping "5/6 built" and folding the verdict to partial while all 5
            # real modules built and tested. A shell is not an unbuildable
            # module; it is scaffolding. We mark it here so the summary can
            # exclude it from the built/total ratio (the row still ships for
            # display/debug).
            #
            # Detected PHYSICALLY, never project-bound: the root must have
            # submodules AND zero own artifacts (0 classes, 0 jars), then the
            # missing packaging semantics confirm it — maven: root pom.xml
            # declares <packaging>pom</packaging>; gradle: the root has no build
            # sources (no src/main). A root WITH its own sources or artifacts
            # (commons-chain's 33 classes) never trips this and stays counted
            # byte-identically. Single-module scans (no submodules) are exempt:
            # the sole module is the project, not a shell.
            if (
                rel == "."
                and len(module_dirs) > 1
                and (class_count or 0) == 0
                and (jar_count or 0) == 0
                and self._is_aggregator_shell_root(module_dir, build_system)
            ):
                record["aggregator_shell"] = True

            modules.append(record)
        return modules

    def _is_aggregator_shell_root(self, root_dir: str, build_system: str) -> bool:
        """Probe whether the root is a pure aggregator with no own sources.

        Confirms the missing packaging semantics for a root that already has
        zero compiled artifacts (the caller guards on that). Maven: the root
        pom.xml declares ``<packaging>pom</packaging>``. Gradle: the root has no
        ``src/main`` build sources. Either signal means the root builds nothing
        of its own and must not count as an unbuilt module. Never raises: an
        unreadable probe returns False, preserving today's counted behavior.
        """
        if build_system == "gradle":
            # No root build sources -> the root aggregates subprojects only.
            chk = self._execute_command_with_logging(
                f"test -d {root_dir}/src/main && echo EXISTS",
                "checking root gradle sources",
            )
            return "EXISTS" not in (chk.get("output") or "")

        # Maven: read <packaging>pom</packaging> from the root pom. grep is
        # whitespace-tolerant; the shell echo makes a match unambiguous.
        chk = self._execute_command_with_logging(
            f"grep -q '<packaging>[[:space:]]*pom[[:space:]]*</packaging>' "
            f"{root_dir}/pom.xml && echo POM",
            "reading root maven packaging",
        )
        return "POM" in (chk.get("output") or "")

    def parse_module_test_reports(self, module_dir: str, report_dirs: List[str]) -> Dict[str, any]:
        """Parse one module's JUnit XML report dirs into counts + failing names.

        Returns {} when the module has no report dirs (test_source -> none).
        Sums testsuite attributes across the module's XML files; failing names
        are testcases containing a <failure> or <error> child.
        """
        if not self.docker_orchestrator or not report_dirs:
            return {}

        import re as _re

        totals = {"tests_total": 0, "tests_failed": 0, "tests_errors": 0, "tests_skipped": 0}
        failing: List[str] = []

        # Collect XML file paths from ALL report dirs, then dedupe by absolute
        # path BEFORE parsing. Gradle's modern layout nests report dirs
        # (build/test-results/test lives inside build/test-results), and
        # scan_modules lists both because older layouts drop XMLs directly in
        # the parent. A recursive `find` on the parent re-lists every file the
        # child find already returned, so parsing per-dir would count each XML
        # twice (live bigtop: 72=2x36, 2=2x1, 26=2x13). File-level dedupe is the
        # robust guarantee: nested (ancestor+descendant) dirs can never
        # double-count, while genuinely distinct files across sibling dirs
        # (Maven surefire + failsafe) are still each counted once.
        seen_files: set = set()
        ordered_files: List[str] = []
        for rd in report_dirs:
            find_cmd = f"find {rd} -name 'TEST-*.xml' -o -name '*.xml' -path '*{rd}*' 2>/dev/null"
            listing = self._execute_command_with_logging(find_cmd, f"listing reports {rd}")
            for f in (listing.get("output") or "").splitlines():
                f = f.strip()
                if not f.endswith(".xml") or f in seen_files:
                    continue
                seen_files.add(f)
                ordered_files.append(f)

        for xml_file in ordered_files:
            cat = self._execute_command_with_logging(f"cat '{xml_file}'", f"reading {xml_file}")
            content = cat.get("output") or ""
            # Parse each <testsuite ...> open tag, reading attributes
            # independently. Surefire and Gradle emit them in different orders
            # (Gradle: name, tests, skipped, failures, errors), so a single
            # positional regex would silently miss one writer's reports.
            for open_tag in _re.finditer(r"<testsuite\b[^>]*>", content):
                tag = open_tag.group(0)
                for key, attr in (
                    ("tests_total", "tests"),
                    ("tests_failed", "failures"),
                    ("tests_errors", "errors"),
                    ("tests_skipped", "skipped"),
                ):
                    m = _re.search(rf'\b{attr}="(\d+)"', tag)
                    if m:
                        totals[key] += int(m.group(1))
            # Failing testcases: match a testcase WITH a body (self-closing
            # passing cases are skipped), then read name/classname from the
            # open tag INDEPENDENTLY. Surefire emits name-before-classname,
            # Gradle classname-before-name -- a positional regex misses one
            # writer (live commons-vfs: failures counted but no names).
            # The `[^/]` before `>` excludes self-closing <testcase .../>
            # (passing cases); otherwise a self-closing tag would be read as
            # an open tag and swallow the next sibling's <failure> body.
            for case in _re.finditer(
                r"<testcase\b([^>]*[^/])>(.*?)</testcase>", content, _re.DOTALL
            ):
                attrs, body = case.group(1), case.group(2)
                if "<failure" in body or "<error" in body:
                    name_m = _re.search(r'\bname="([^"]*)"', attrs)
                    cls_m = _re.search(r'\bclassname="([^"]*)"', attrs)
                    nm = name_m.group(1) if name_m else "(unknown)"
                    cls = cls_m.group(1) if cls_m else ""
                    failing.append(f"{cls}.{nm}" if cls else nm)

        passed = max(
            totals["tests_total"]
            - totals["tests_failed"]
            - totals["tests_errors"]
            - totals["tests_skipped"],
            0,
        )
        # failing_count is authoritative (failures + errors from the testsuite
        # attrs), independent of how many per-case names we could extract. The
        # name list is best-effort; the count must never collapse to 0 when
        # tests actually failed.
        return {
            "tests_total": totals["tests_total"],
            "tests_passed": passed,
            "tests_failed": totals["tests_failed"],
            "tests_errors": totals["tests_errors"],
            "tests_skipped": totals["tests_skipped"],
            "failing_names": failing,
            "failing_count": totals["tests_failed"] + totals["tests_errors"],
            "evidence_refs": list(report_dirs),
        }

    def _validate_maven_fingerprints(self, project_dir: str) -> Dict[str, any]:
        """
        Validate Maven build fingerprints without executing mvn commands.

        Checks:
        - target/maven-status/ directories
        - target/maven-archiver/pom.properties
        - Multi-module fingerprints
        """
        result = {"valid": False, "details": {}, "modules": []}

        # Check main project fingerprints
        fingerprint_checks = [
            f"{project_dir}/target/maven-status/maven-compiler-plugin/compile/default-compile/createdFiles.lst",
            f"{project_dir}/target/maven-archiver/pom.properties",
            f"{project_dir}/target/classes",
        ]

        fingerprints_found = 0
        for fingerprint_path in fingerprint_checks:
            cmd = f"test -e {fingerprint_path}"
            check_result = self._execute_command_with_logging(cmd, f"checking {fingerprint_path}")
            if check_result["success"]:
                fingerprints_found += 1
                result["details"][fingerprint_path.split("/")[-1]] = True

        # Check for multi-module projects
        modules_cmd = (
            f"find {project_dir} -mindepth 2 -maxdepth 2 -name 'pom.xml' -type f 2>/dev/null"
        )
        modules_result = self._execute_command_with_logging(modules_cmd, "finding Maven modules")
        if modules_result["success"] and modules_result["output"]:
            modules = modules_result["output"].strip().split("\n")
            for module_pom in modules:
                if module_pom:
                    module_dir = "/".join(module_pom.split("/")[:-1])
                    module_name = module_dir.split("/")[-1]

                    # Check module fingerprints
                    module_target = f"{module_dir}/target/maven-status"
                    cmd = f"test -d {module_target}"
                    if self._execute_command_with_logging(cmd, f"checking module {module_name}")[
                        "success"
                    ]:
                        result["modules"].append(module_name)

        # Determine validity
        result["valid"] = fingerprints_found > 0 or len(result["modules"]) > 0

        return result

    def _get_expected_artifacts(self, project_dir: str, build_system: str) -> List[Dict[str, str]]:
        """
        Parse build configuration to determine expected artifacts.

        Returns:
            List of expected artifacts with paths and types
        """
        expected = []

        if build_system == "maven":
            expected = self._parse_maven_expected_artifacts(project_dir)
        elif build_system == "gradle":
            expected = self._parse_gradle_expected_artifacts(project_dir)

        return expected

    @staticmethod
    def _path_is_within(path: str, root: str) -> bool:
        return path == root or path.startswith(root.rstrip("/") + "/")

    def _existing_container_realpath(self, path: str) -> Optional[str]:
        """Return one normalized absolute realpath, or ``None`` when unproved."""
        normalized = posixpath.normpath(str(path or "").strip())
        if not normalized.startswith("/") or any(char in normalized for char in "\x00\r\n"):
            return None
        result = self._execute_command_with_logging(
            f"realpath -e -- {shlex.quote(normalized)}",
            "resolving Maven reactor path",
        )
        if not result.get("success"):
            return None
        lines = (result.get("output") or "").splitlines()
        if len(lines) != 1:
            return None
        resolved = posixpath.normpath(lines[0].strip())
        if not resolved.startswith("/") or any(char in resolved for char in "\x00\r\n"):
            return None
        return resolved

    @staticmethod
    def _profile_marker_present(command: str) -> bool:
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            tokens = []
        if any(
            token in {"-P", "--activate-profiles"}
            or (token.startswith("-P") and token != "-P")
            or token.startswith("--activate-profiles=")
            for token in tokens
        ):
            return True
        return bool(
            re.search(
                r"(?<!\S)(?:-P(?:\S*)?|--activate-profiles(?:=\S*|\s|$))",
                command,
            )
        )

    @staticmethod
    def _parse_maven_profile_values(
        values: List[str],
    ) -> Optional[Tuple[frozenset[str], frozenset[str]]]:
        enabled: set[str] = set()
        disabled: set[str] = set()
        for value in values:
            for raw_item in value.split(","):
                item = raw_item.strip()
                if not item:
                    return None
                target = enabled
                if item[0] in {"!", "-"}:
                    target = disabled
                    item = item[1:]
                if (
                    not item
                    or item[0] in {"!", "-"}
                    or any(char.isspace() or char in "\x00\r\n," for char in item)
                ):
                    return None
                target.add(item)
        if enabled & disabled:
            return None
        return frozenset(enabled), frozenset(disabled)

    def _maven_receipt_binding(
        self,
        receipt: Dict[str, any],
        project_dir: str,
    ) -> Tuple[Optional[bool], Optional[List[str]], Optional[str]]:
        """Return exact reactor binding plus Maven launcher's ``.mvn`` seed."""
        command = str(receipt.get("command") or "").strip()
        tool = str(receipt.get("tool") or "").strip().lower()
        if not command or (tool and tool != "maven"):
            return False, None, None

        working_dir = posixpath.normpath(str(receipt.get("working_dir") or "").strip())
        working_dir = working_dir if working_dir.startswith("/") else None
        marker = self._profile_marker_present(command)

        def same_existing_path(candidate: str, expected: str) -> Optional[bool]:
            if candidate == expected:
                return True
            if not self.docker_orchestrator:
                return False
            candidate_real = self._existing_container_realpath(candidate)
            expected_real = self._existing_container_realpath(expected)
            if candidate_real is None or expected_real is None:
                return None
            return candidate_real == expected_real

        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            cwd_binding = (
                same_existing_path(working_dir, project_dir) if working_dir is not None else False
            )
            return (
                (None if marker and cwd_binding is not False else False),
                None,
                working_dir,
            )

        executable_indexes = [
            index
            for index, token in enumerate(tokens)
            if posixpath.basename(token) in {"mvn", "mvnw"}
        ]
        if not executable_indexes:
            cwd_binding = (
                same_existing_path(working_dir, project_dir) if working_dir is not None else False
            )
            if tool == "maven" and cwd_binding is True:
                return (None if marker else True), tokens, working_dir
            if marker and cwd_binding is None:
                return None, tokens, working_dir
            return False, tokens, working_dir
        if len(executable_indexes) != 1:
            cwd_binding = (
                same_existing_path(working_dir, project_dir) if working_dir is not None else False
            )
            return (
                (None if marker and cwd_binding is not False else False),
                tokens,
                working_dir,
            )

        pom_values: List[Tuple[str, bool]] = []
        index = executable_indexes[0] + 1
        while index < len(tokens):
            token = tokens[index]
            if token in {"-f", "--file"}:
                if index + 1 >= len(tokens):
                    return None, tokens, working_dir
                pom_values.append((tokens[index + 1], True))
                index += 2
                continue
            if token.startswith("--file="):
                pom_values.append((token.split("=", 1)[1], False))
            elif token.startswith("-f") and token not in {"-f", "-fae", "-ff", "-fn"}:
                pom_values.append((token[2:], False))
            index += 1

        if pom_values:
            if working_dir is None:
                return None, tokens, working_dir
            normalized_poms: set[str] = set()
            separated_seeds: set[str] = set()
            for value, separated in pom_values:
                if not value or any(char in value for char in "\x00\r\n"):
                    return None, tokens, working_dir
                candidate = value if value.startswith("/") else posixpath.join(working_dir, value)
                candidate = posixpath.normpath(candidate)
                directory_state = None
                if self.docker_orchestrator:
                    probe = self._execute_command_with_logging(
                        f"test -d {shlex.quote(candidate)}",
                        "checking Maven -f target type",
                    )
                    if probe.get("success"):
                        directory_state = True
                    elif probe.get("exit_code") == 1:
                        directory_state = False
                    else:
                        return None, tokens, working_dir
                if directory_state is None:
                    directory_state = (
                        candidate == project_dir
                        or not posixpath.splitext(posixpath.basename(candidate))[1]
                    )
                pom_candidate = (
                    posixpath.join(candidate, "pom.xml") if directory_state else candidate
                )
                normalized_poms.add(pom_candidate)
                if separated:
                    separated_seeds.add(
                        candidate if directory_state else posixpath.dirname(candidate)
                    )
            if len(normalized_poms) != 1:
                return None, tokens, working_dir
            binding = same_existing_path(
                next(iter(normalized_poms)),
                posixpath.join(project_dir, "pom.xml"),
            )
            if binding is None:
                return None, tokens, working_dir
            if not binding:
                return False, tokens, working_dir
            if len(separated_seeds) > 1:
                return None, tokens, working_dir
            launcher_basedir = next(iter(separated_seeds)) if separated_seeds else working_dir
            return (
                True,
                tokens,
                launcher_basedir,
            )

        if working_dir is None:
            return (None if marker else False), tokens, working_dir
        cwd_binding = same_existing_path(working_dir, project_dir)
        return cwd_binding, tokens, working_dir

    def _tracked_maven_receipts(self) -> List[Dict[str, any]]:
        command_tracker = getattr(self, "command_tracker", None)
        if command_tracker is None:
            return []
        receipts: List[Dict[str, any]] = []
        for method_name in (
            "get_all_execution_receipts",
            "get_all_build_commands",
            "get_all_test_commands",
        ):
            method = getattr(command_tracker, method_name, None)
            if not callable(method):
                continue
            values = method()
            if isinstance(values, list):
                receipts.extend(
                    value
                    for value in values
                    if isinstance(value, dict)
                    and (
                        method_name != "get_all_execution_receipts"
                        or value.get("runner_dispatched") is True
                    )
                )
        if receipts:
            return receipts
        for method_name in ("get_last_build_command", "get_last_test_command"):
            method = getattr(command_tracker, method_name, None)
            value = method() if callable(method) else None
            if isinstance(value, dict):
                receipts.append(value)
        return receipts

    def _recorded_maven_profile_selection(
        self,
        project_dir: str,
    ) -> _MavenProfileSelection:
        """Profiles from the newest build/test receipt bound to this reactor."""
        relevant: List[Tuple[Dict[str, any], List[str], str]] = []
        ambiguous_binding = False
        for receipt in self._tracked_maven_receipts():
            binding, tokens, working_dir = self._maven_receipt_binding(
                receipt,
                project_dir,
            )
            if binding is None:
                ambiguous_binding = True
            elif binding is True:
                relevant.append(
                    (
                        receipt,
                        tokens or [],
                        working_dir or project_dir,
                    )
                )

        unresolved = _MavenProfileSelection(
            explicit=True,
            conflicts=("maven_profile_selection_unresolved",),
            working_dir=project_dir,
        )
        if ambiguous_binding:
            return unresolved
        if not relevant:
            return _MavenProfileSelection(working_dir=project_dir)
        if len(relevant) > 1:
            timestamps = [str(item[0].get("timestamp") or "") for item in relevant]
            if not all(timestamps):
                if any(
                    self._profile_marker_present(str(item[0].get("command") or ""))
                    for item in relevant
                ):
                    return unresolved
                return _MavenProfileSelection(working_dir=project_dir)
            newest = max(timestamps)
            latest = [item for item, timestamp in zip(relevant, timestamps) if timestamp == newest]
            if len(latest) != 1:
                return unresolved
            receipt, tokens, working_dir = latest[0]
        else:
            receipt, tokens, working_dir = relevant[0]

        command = str(receipt.get("command") or "").strip()
        dispatch_status = str(receipt.get("dispatch_status") or "").strip()
        invocation_status = str(receipt.get("invocation_status") or "").strip()
        unfinished = invocation_status == "pending" or dispatch_status in {
            "running_detached",
            "liveness_unknown_detached",
        }
        receipt_conflicts = ("maven_profile_execution_unfinished",) if unfinished else ()
        if not self._profile_marker_present(command):
            return _MavenProfileSelection(
                conservative=unfinished,
                conflicts=receipt_conflicts,
                working_dir=working_dir,
            )
        if any(token in {"&&", "||", ";", "|", "&"} for token in tokens):
            return unresolved
        executable_indexes = [
            index
            for index, token in enumerate(tokens)
            if posixpath.basename(token) in {"mvn", "mvnw"}
        ]
        if len(executable_indexes) != 1:
            return unresolved

        values: List[str] = []
        index = executable_indexes[0] + 1
        while index < len(tokens):
            token = tokens[index]
            if token in {"-P", "--activate-profiles"}:
                if index + 1 >= len(tokens):
                    return unresolved
                values.append(tokens[index + 1])
                index += 2
                continue
            if token.startswith("-P") and token != "-P":
                values.append(token[2:])
            elif token.startswith("--activate-profiles="):
                values.append(token.split("=", 1)[1])
            elif token.startswith("--activate-profiles"):
                return unresolved
            index += 1

        parsed = self._parse_maven_profile_values(values) if values else None
        if parsed is None:
            return unresolved
        enabled, disabled = parsed
        return _MavenProfileSelection(
            enabled=enabled,
            disabled=disabled,
            explicit=True,
            conservative=unfinished,
            conflicts=receipt_conflicts,
            working_dir=working_dir,
        )

    @staticmethod
    def _direct_maven_modules(
        pom_content: str,
        profile_selection: _MavenProfileSelection,
    ) -> Optional[_MavenModuleDeclarations]:
        """Resolve the statically active Maven module declarations.

        Explicit profiles come from the exact last Maven build receipt. Without
        such a receipt, direct modules and profiles activated *only* by
        ``activeByDefault=true`` are active. Module-bearing profiles with
        JDK/OS/property/file (or unknown) activation are runtime-dependent and
        make the snapshot incomplete.
        """
        try:
            root = ET.fromstring(pom_content)
        except ET.ParseError:
            return None

        def local_name(element: ET.Element) -> str:
            return element.tag.rsplit("}", 1)[-1]

        if local_name(root) != "project":
            return None

        def module_values(block: ET.Element) -> List[str]:
            values: List[str] = []
            for module in block:
                if local_name(module) != "module":
                    continue
                value = str(module.text or "").strip()
                if value:
                    values.append(value)
            return values

        modules: List[str] = []
        conflicts: set[str] = set()
        for block in root:
            if local_name(block) == "modules":
                modules.extend(module_values(block))

        for profiles in root:
            if local_name(profiles) != "profiles":
                continue
            profile_nodes = [profile for profile in profiles if local_name(profile) == "profile"]
            profile_ids = [
                str(identifier.text or "").strip()
                for profile in profile_nodes
                for identifier in profile
                if local_name(identifier) == "id" and str(identifier.text or "").strip()
            ]
            explicit_in_pom = profile_selection.enabled.intersection(profile_ids)
            for profile in profile_nodes:
                identifiers = [
                    str(child.text or "").strip() for child in profile if local_name(child) == "id"
                ]
                profile_modules = [child for child in profile if local_name(child) == "modules"]
                if not profile_modules:
                    continue
                declared = [value for block in profile_modules for value in module_values(block)]
                if not declared:
                    continue

                if len(identifiers) != 1 or not identifiers[0]:
                    conflicts.add("maven_profile_activation_unresolved")
                    continue
                profile_id = identifiers[0]
                if profile_id in profile_selection.disabled and not profile_selection.conservative:
                    continue
                if profile_id in profile_selection.enabled:
                    modules.extend(declared)
                    continue

                activations = [child for child in profile if local_name(child) == "activation"]
                if not activations:
                    # No matching -P selector was recorded by this physical judge.
                    continue
                if len(activations) != 1:
                    conflicts.add("maven_profile_activation_unresolved")
                    continue

                activation_children = list(activations[0])
                dynamic = [
                    child for child in activation_children if local_name(child) != "activeByDefault"
                ]
                defaults = [
                    child for child in activation_children if local_name(child) == "activeByDefault"
                ]
                if dynamic or len(defaults) > 1:
                    conflicts.add("maven_profile_activation_unresolved")
                    continue
                if not defaults:
                    # An empty activation block does not activate the profile.
                    continue
                active_text = str(defaults[0].text or "").strip().lower()
                if active_text == "true":
                    # Maven disables activeByDefault when another profile in
                    # the same POM was explicitly activated.
                    if not explicit_in_pom or profile_selection.conservative:
                        modules.extend(declared)
                elif active_text != "false":
                    conflicts.add("maven_profile_activation_unresolved")

        return _MavenModuleDeclarations(
            modules=tuple(modules),
            conflicts=tuple(sorted(conflicts)),
        )

    @staticmethod
    def _maven_module_lexical_path(module_dir: str, raw_module: str) -> Optional[str]:
        value = str(raw_module or "").strip()
        value = value.replace("${project.basedir}", module_dir)
        value = value.replace("${basedir}", module_dir)
        if not value or "${" in value or any(char in value for char in "\x00\r\n"):
            return None
        if not value.startswith("/"):
            value = posixpath.join(module_dir, value)
        return posixpath.normpath(value)

    def _maven_config_conflicts(
        self,
        project_real: str,
        profile_selection: _MavenProfileSelection,
    ) -> set[str]:
        """Check the nearest Maven launcher config without following symlinks out."""
        basedir = profile_selection.working_dir or project_real
        basedir_real = self._existing_container_realpath(basedir)
        if basedir_real is None:
            return {"maven_config_unreadable"}

        current = basedir_real
        while True:
            maven_dir = posixpath.join(current, ".mvn")
            directory = self._execute_command_with_logging(
                f"test -d {shlex.quote(maven_dir)}",
                "checking Maven launcher directory",
            )
            if directory.get("success"):
                maven_dir_real = self._existing_container_realpath(maven_dir)
                if maven_dir_real is None:
                    return {"maven_config_unreadable"}
                if not self._path_is_within(maven_dir_real, project_real):
                    return {"maven_config_outside_project"}

                config_path = posixpath.join(maven_dir, "maven.config")
                config_exists = self._execute_command_with_logging(
                    f"test -f {shlex.quote(config_path)}",
                    "checking Maven launcher config",
                )
                if not config_exists.get("success"):
                    if config_exists.get("exit_code") != 1:
                        return {"maven_config_unreadable"}
                    return set()
                config_real = self._existing_container_realpath(config_path)
                if config_real is None:
                    return {"maven_config_unreadable"}
                if not self._path_is_within(config_real, project_real):
                    return {"maven_config_outside_project"}
                config = self._execute_command_with_logging(
                    f"cat {shlex.quote(config_real)}",
                    "reading Maven launcher config",
                )
                if not config.get("success"):
                    return {"maven_config_unreadable"}
                try:
                    from sag.agent.forced_build_graph import (
                        _maven_config_changes_graph,
                    )

                    changes_graph = _maven_config_changes_graph(str(config.get("output") or ""))
                except Exception:
                    return {"maven_config_unreadable"}
                return {"maven_config_changes_graph"} if changes_graph else set()

            if directory.get("exit_code") != 1:
                return {"maven_config_unreadable"}
            parent = posixpath.dirname(current)
            if parent == current:
                return set()
            current = parent

    def _bounded_maven_reactor(
        self,
        project_dir: str,
    ) -> _MavenReactorSnapshot:
        """Build one boundary-checked snapshot for all Maven evidence consumers.

        Unsafe/unreadable child branches are omitted from ``records`` but make
        ``complete`` false. Consumers can therefore inspect safe evidence while
        the build verdict remains non-green: rejecting a module must never
        silently shrink the expected-artifact denominator.
        """
        normalized_project = posixpath.normpath(str(project_dir or "").strip())
        normalized_workspace = posixpath.normpath(str(self.project_path or "").strip())
        profile_selection = self._recorded_maven_profile_selection(
            normalized_project,
        )
        selection_conflicts = set(profile_selection.conflicts)
        if not self.docker_orchestrator:
            return self._maven_reactor_snapshot(
                [
                    _MavenReactorRecord(
                        normalized_project.rstrip("/") or "/",
                        "",
                        False,
                    )
                ],
                {
                    *selection_conflicts,
                    "maven_reactor_probe_unavailable",
                },
                profile_selection,
            )
        if (
            not normalized_project.startswith("/")
            or not normalized_workspace.startswith("/")
            or not self._path_is_within(normalized_project, normalized_workspace)
        ):
            logger.warning("Maven reactor root is outside the validator workspace")
            return self._maven_reactor_snapshot(
                [],
                {
                    *selection_conflicts,
                    "maven_reactor_root_unverified",
                },
                profile_selection,
            )

        workspace_real = self._existing_container_realpath(normalized_workspace)
        project_real = self._existing_container_realpath(normalized_project)
        if (
            workspace_real is None
            or project_real is None
            or not self._path_is_within(project_real, workspace_real)
        ):
            logger.warning("Maven reactor root realpath could not be verified")
            return self._maven_reactor_snapshot(
                [],
                {
                    *selection_conflicts,
                    "maven_reactor_root_unverified",
                },
                profile_selection,
            )

        probed: set[str] = set()
        cache: Dict[str, List[_MavenReactorRecord]] = {}
        stack: List[str] = []
        root_pom = ""
        graph_conflicts: set[str] = set(selection_conflicts)
        graph_conflicts.update(
            self._maven_config_conflicts(
                project_real,
                profile_selection,
            )
        )

        def read_pom(module_real: str) -> Tuple[Optional[str], Optional[str]]:
            pom_path = posixpath.join(module_real, "pom.xml")
            pom_real = self._existing_container_realpath(pom_path)
            if pom_real is None:
                return None, "maven_module_pom_unreadable"
            if not self._path_is_within(pom_real, project_real):
                return None, "maven_module_pom_outside_project"
            try:
                content = read_container_text(self.docker_orchestrator, pom_real)
            except ContainerFileReadError:
                return None, "maven_module_pom_unreadable"
            if content is None:
                return None, "maven_module_pom_unreadable"
            return content, None

        def walk(module_path: str, depth: int) -> List[_MavenReactorRecord]:
            nonlocal root_pom
            if depth > _MAVEN_REACTOR_MAX_DEPTH:
                raise _MavenReactorLimit("maven_module_depth_exceeded")

            lexical = posixpath.normpath(module_path)
            if not self._path_is_within(lexical, project_real):
                logger.warning(f"Ignoring Maven module outside project: {lexical}")
                graph_conflicts.add("maven_module_outside_project")
                return []
            module_real = self._existing_container_realpath(lexical)
            if module_real and module_real.endswith("/pom.xml"):
                module_real = posixpath.dirname(module_real)
            if module_real is None or not self._path_is_within(module_real, project_real):
                logger.warning(f"Ignoring unverified Maven module: {lexical}")
                graph_conflicts.add("maven_module_unresolved")
                return []

            if module_real in stack:
                cycle_start = stack.index(module_real)
                raise _MavenReactorCycle(frozenset(stack[cycle_start:]))
            if module_real in cache:
                return cache[module_real]
            if module_real not in probed:
                if len(probed) >= _MAVEN_REACTOR_MAX_NODES:
                    raise _MavenReactorLimit("maven_module_cap_exceeded")
                probed.add(module_real)

            pom_content, pom_conflict = read_pom(module_real)
            if pom_content is None:
                graph_conflicts.add(pom_conflict or "maven_module_pom_unreadable")
                return (
                    [_MavenReactorRecord(module_real, "", False)]
                    if module_real == project_real
                    else []
                )
            if module_real == project_real:
                root_pom = pom_content

            stack.append(module_real)
            children: List[_MavenReactorRecord] = []
            try:
                declarations = self._direct_maven_modules(
                    pom_content,
                    profile_selection,
                )
                if declarations is None:
                    graph_conflicts.add("maven_module_pom_invalid")
                    declarations = _MavenModuleDeclarations(())
                graph_conflicts.update(declarations.conflicts)
                for raw_module in declarations.modules:
                    child_lexical = self._maven_module_lexical_path(module_real, raw_module)
                    if child_lexical is None:
                        logger.warning(f"Ignoring unresolved Maven module: {raw_module!r}")
                        graph_conflicts.add("maven_module_unresolved")
                        continue
                    try:
                        children.extend(walk(child_lexical, depth + 1))
                    except _MavenReactorCycle as exc:
                        graph_conflicts.add("maven_module_cycle")
                        if module_real in exc.members:
                            raise
                        logger.warning("Ignoring cyclic Maven module branch")
            finally:
                stack.pop()

            records = [
                _MavenReactorRecord(
                    module_real,
                    pom_content,
                    bool(children),
                ),
                *children,
            ]
            cache[module_real] = records
            return records

        try:
            records = walk(project_real, 0)
        except _MavenReactorCycle as exc:
            graph_conflicts.add("maven_module_cycle")
            logger.warning(f"Discarding unsafe Maven reactor branches: {exc}")
            records = [_MavenReactorRecord(project_real, root_pom, False)] if root_pom else []
        except _MavenReactorLimit as exc:
            graph_conflicts.add(exc.reason_code)
            logger.warning(f"Discarding unsafe Maven reactor branches: {exc}")
            records = [_MavenReactorRecord(project_real, root_pom, False)] if root_pom else []

        deduped: List[_MavenReactorRecord] = []
        seen_realpaths: set[str] = set()
        for record in records:
            if record.module_dir in seen_realpaths:
                continue
            seen_realpaths.add(record.module_dir)
            deduped.append(record)
        return self._maven_reactor_snapshot(
            deduped,
            graph_conflicts,
            profile_selection,
        )

    @staticmethod
    def _maven_reactor_snapshot(
        records: List[_MavenReactorRecord],
        conflicts: set[str],
        profile_selection: _MavenProfileSelection,
    ) -> _MavenReactorSnapshot:
        ordered_conflicts = tuple(sorted(conflicts))
        reason = "; ".join(
            _MAVEN_REACTOR_CONFLICT_REASONS.get(conflict, conflict)
            for conflict in ordered_conflicts
        )
        return _MavenReactorSnapshot(
            records=tuple(records),
            complete=not ordered_conflicts,
            conflicts=ordered_conflicts,
            reason=reason,
            profile_selection=profile_selection,
        )

    def _active_maven_module_dirs(self, project_dir: str) -> List[str]:
        """Return the shared, bounded reactor snapshot's verified directories."""
        snapshot = self._bounded_maven_reactor(project_dir)
        return [record.module_dir for record in snapshot.records]

    def _parse_maven_expected_artifacts(
        self,
        project_dir: str,
        *,
        snapshot: Optional[_MavenReactorSnapshot] = None,
    ) -> List[Dict[str, str]]:
        """Parse expected artifacts from the shared, verified reactor snapshot."""
        snapshot = snapshot or self._bounded_maven_reactor(project_dir)
        expected: List[Dict[str, str]] = []
        artifact_conflicts = set(snapshot.conflicts)
        for record in snapshot.records:
            # Preserve the historical rule: aggregator nodes with active modules
            # contribute no direct artifact expectation; their safe leaves do.
            if not record.has_safe_children:
                module_expected = self._parse_single_maven_expected_artifacts(
                    record.module_dir,
                    record.pom_content,
                )
                expected.extend(module_expected)
                packaging = self._maven_pom_packaging(record.pom_content)
                if not module_expected and packaging not in {None, "pom"}:
                    # Inherited/effective versions can make a real JAR leaf
                    # impossible to name statically. Silence would shrink the
                    # denominator; fail closed until pom.properties or a
                    # matching built artifact makes the expectation concrete.
                    artifact_conflicts.add("maven_module_artifact_unresolved")
        if artifact_conflicts != set(snapshot.conflicts):
            snapshot = self._maven_reactor_snapshot(
                list(snapshot.records),
                artifact_conflicts,
                snapshot.profile_selection,
            )
        return _MavenArtifactExpectations(expected, snapshot)

    @staticmethod
    def _maven_pom_packaging(pom_content: str) -> Optional[str]:
        """Return one verified leaf packaging, defaulting as Maven does to JAR."""
        try:
            root = ET.fromstring(pom_content)
        except ET.ParseError:
            return None
        if root.tag.rsplit("}", 1)[-1] != "project":
            return None
        values = [
            str(child.text or "").strip().lower()
            for child in root
            if child.tag.rsplit("}", 1)[-1] == "packaging"
        ]
        if len(values) > 1 or (values and not values[0]):
            return None
        return values[0] if values else "jar"

    def _parse_single_maven_expected_artifacts(
        self,
        project_dir: str,
        pom_content: str,
    ) -> List[Dict[str, str]]:
        """
        Parse one already-verified Maven module without following module edges.
        """
        expected: List[Dict[str, str]] = []

        # Initialize variables
        artifact_id = None
        version = None
        packaging = "jar"  # default

        # Strategy 1: Enhanced regex with more truncation anchors
        try:
            # Remove parent section to avoid matching parent artifactId
            pom_without_parent = re.sub(r"<parent>.*?</parent>", "", pom_content, flags=re.DOTALL)

            # Apply multiple truncation anchors to isolate project definition
            # Each split removes potential interference from later sections
            project_section = pom_without_parent

            # More comprehensive list of sections to truncate
            truncation_points = [
                "<dependencies>",
                "<dependencyManagement>",
                "<build>",
                "<reporting>",
                "<profiles>",
                "<pluginManagement>",
                "<properties>",
                "<distributionManagement>",
                "<repositories>",
            ]

            for truncation_point in truncation_points:
                if truncation_point in project_section:
                    project_section = project_section.split(truncation_point)[0]

            # Extract artifactId from isolated project section
            artifact_match = re.search(r"<artifactId>([^<]+)</artifactId>", project_section)
            artifact_id = artifact_match.group(1).strip() if artifact_match else None

            # Extract version (might be inherited from parent)
            version_match = re.search(r"<version>([^<]+)</version>", project_section)
            version = version_match.group(1).strip() if version_match else None

            # Extract packaging
            packaging_match = re.search(r"<packaging>([^<]+)</packaging>", project_section)
            packaging = packaging_match.group(1).strip() if packaging_match else "jar"

        except Exception as e:
            logger.debug(f"Regex parsing failed: {e}, trying XML parsing fallback")

        # Strategy 2: XML parsing fallback (lightweight, no external dependencies)
        if not artifact_id:
            try:
                import xml.etree.ElementTree as ET

                # Parse POM as XML, handling namespaces
                root = ET.fromstring(pom_content)

                # Handle default Maven namespace
                ns = {"m": "http://maven.apache.org/POM/4.0.0"}

                # Try with namespace first
                artifact_elem = root.find("m:artifactId", ns)
                if artifact_elem is None:
                    # Try without namespace
                    artifact_elem = root.find("artifactId")

                if artifact_elem is not None and artifact_elem.text:
                    artifact_id = artifact_elem.text.strip()

                # Get version
                version_elem = root.find("m:version", ns)
                if version_elem is None:
                    version_elem = root.find("version")

                if version_elem is not None and version_elem.text:
                    version = version_elem.text.strip()

                # Get packaging
                packaging_elem = root.find("m:packaging", ns)
                if packaging_elem is None:
                    packaging_elem = root.find("packaging")

                if packaging_elem is not None and packaging_elem.text:
                    packaging = packaging_elem.text.strip()

            except Exception as e:
                logger.debug(f"XML parsing fallback failed: {e}")

        # Strategy 3: Read version from pom.properties if still missing
        if artifact_id and not version:
            pom_props_cmd = f"cat {project_dir}/target/maven-archiver/pom.properties 2>/dev/null"
            pom_props_result = self._execute_command_with_logging(
                pom_props_cmd, "reading pom.properties for version"
            )

            if pom_props_result["success"]:
                # Parse properties file
                for line in pom_props_result["output"].split("\n"):
                    if line.startswith("version="):
                        version = line.split("=", 1)[1].strip()
                        logger.debug(f"Retrieved version {version} from pom.properties")
                        break

        # Strategy 4: Try to infer version from existing JARs if still missing
        if artifact_id and not version and packaging != "pom":
            # Look for existing JAR that matches the pattern
            jar_search_cmd = (
                f"ls {project_dir}/target/{artifact_id}-*.{packaging} 2>/dev/null | head -1"
            )
            jar_result = self._execute_command_with_logging(
                jar_search_cmd, f"searching for {artifact_id} JAR"
            )

            if jar_result["success"] and jar_result["output"]:
                # Extract version from filename
                import os

                jar_name = os.path.basename(jar_result["output"].strip())
                # Pattern: artifactId-version.packaging
                version_pattern = f"{artifact_id}-(.+)\\.{packaging}"
                version_match = re.match(version_pattern, jar_name)
                if version_match:
                    version = version_match.group(1)
                    logger.debug(f"Inferred version {version} from existing JAR: {jar_name}")

        if packaging == "pom":
            # Parent POM doesn't produce artifacts but might coordinate compilation.
            return expected

        # Expected .class files
        src_main_check = f"test -d {project_dir}/src/main/java && echo EXISTS"
        src_main_result = self._execute_command_with_logging(
            src_main_check, "checking src/main/java"
        )

        if src_main_result["success"] and "EXISTS" in src_main_result.get("output", ""):
            # Count Java source files to expect corresponding class files.
            java_count_cmd = (
                f"find {project_dir}/src/main/java -name '*.java' " "-type f 2>/dev/null | wc -l"
            )
            java_count_result = self._execute_command_with_logging(
                java_count_cmd, "counting Java sources"
            )

            if java_count_result["success"]:
                java_count = int(java_count_result["output"].strip() or 0)
                if java_count > 0:
                    expected.append(
                        {
                            "path": f"{project_dir}/target/classes",
                            "type": "classes",
                            "artifact": (f"compiled classes (from {java_count} source files)"),
                            "min_count": java_count,
                        }
                    )

        # Expected JAR/WAR artifact. A coordinate that still carries a Maven
        # property (`${revision}`, `${sha1}`, `${changelist}` — the CI-friendly
        # versions) was never resolved, so the path it produces can never
        # exist. Live ignite: `ignite-checkstyle-${revision}.jar` was reported
        # missing on every run, a permanent shortfall no build could close.
        # An expectation we cannot state is not an expectation.
        if (
            artifact_id
            and version
            and not _carries_unresolved_property(f"{artifact_id}{version}{packaging}")
        ):
            expected_path = f"{project_dir}/target/{artifact_id}-{version}.{packaging}"
            expected.append(
                {
                    "path": expected_path,
                    "type": packaging,
                    "artifact": f"{artifact_id}-{version}.{packaging}",
                }
            )

        return expected

    def _declared_gradle_subprojects(self, project_dir: str) -> List[str]:
        """The subprojects settings.gradle(.kts) declares, as relative dirs.

        The single reader of the settings file (P3): the expected-artifact walk
        and `scan_modules` both come here, so the modules a build DECLARES and
        the modules the scan COUNTS are one list. Reads the Kotlin DSL too —
        p7d polaris declared 26 subprojects in `settings.gradle.kts` and nothing
        read it. Cached per project dir: the scan asks once per build system.
        """
        cache_key = self._get_cache_key("gradle_subprojects", project_dir)
        cached = self._get_cached_result(cache_key)
        if cached is not None:
            return list(cached)

        content = ""
        for name in ("settings.gradle", "settings.gradle.kts"):
            result = self._execute_command_with_logging(
                f"cat {project_dir}/{name} 2>/dev/null", f"reading {name}"
            )
            if result["success"] and (result.get("output") or "").strip():
                content = result["output"]
                break

        declared = _parse_gradle_include_paths(content)
        self._cache_result(cache_key, declared)
        return list(declared)

    def _parse_gradle_expected_artifacts(self, project_dir: str) -> List[Dict[str, str]]:
        """
        Parse build.gradle to determine expected Gradle artifacts including .class files.
        """
        expected = []

        # Check for build.gradle or build.gradle.kts
        gradle_files = ["build.gradle", "build.gradle.kts"]
        gradle_content = None

        for gradle_file in gradle_files:
            cmd = f"cat {project_dir}/{gradle_file} 2>/dev/null"
            result = self._execute_command_with_logging(cmd, f"reading {gradle_file}")
            if result["success"]:
                gradle_content = result["output"]
                break

        if not gradle_content:
            return expected

        # Check if it's a Java/Kotlin project
        if "java" in gradle_content or "kotlin" in gradle_content:
            # The declared subprojects, read through the ONE settings parse the
            # module scan also uses (P3) — so a subproject can never be expected
            # here and absent from the denominator there.
            includes = self._declared_gradle_subprojects(project_dir)

            if includes:
                for subproject_path in includes:
                    subproject_dir = f"{project_dir}/{subproject_path}"

                    # Expected .class files for subproject
                    src_check = f"test -d {subproject_dir}/src/main/java && echo EXISTS"
                    src_result = self._execute_command_with_logging(
                        src_check, f"checking {subproject_path} sources"
                    )

                    if src_result["success"] and "EXISTS" in src_result.get("output", ""):
                        # Count Java sources
                        count_cmd = f"find {subproject_dir}/src/main/java -name '*.java' -type f 2>/dev/null | wc -l"
                        count_result = self._execute_command_with_logging(
                            count_cmd, f"counting {subproject_path} sources"
                        )

                        if count_result["success"]:
                            java_count = int(count_result["output"].strip() or 0)
                            if java_count > 0:
                                expected.append(
                                    {
                                        "path": f"{subproject_dir}/build/classes/java/main",
                                        "type": "classes",
                                        "artifact": f"{subproject_path} classes ({java_count} sources)",
                                        "min_count": java_count,
                                    }
                                )

                    # Expected JAR for subproject
                    expected.append(
                        {
                            "path": f"{subproject_dir}/build/libs",
                            "type": "jar",
                            "artifact": f"{subproject_path} JAR",
                        }
                    )
            else:
                # Single project
                # Check for Java sources
                src_check = f"test -d {project_dir}/src/main/java && echo EXISTS"
                src_result = self._execute_command_with_logging(src_check, "checking main sources")

                if src_result["success"] and "EXISTS" in src_result.get("output", ""):
                    # Count Java sources
                    count_cmd = f"find {project_dir}/src/main/java -name '*.java' -type f 2>/dev/null | wc -l"
                    count_result = self._execute_command_with_logging(
                        count_cmd, "counting main sources"
                    )

                    if count_result["success"]:
                        java_count = int(count_result["output"].strip() or 0)
                        if java_count > 0:
                            expected.append(
                                {
                                    "path": f"{project_dir}/build/classes/java/main",
                                    "type": "classes",
                                    "artifact": f"compiled classes ({java_count} sources)",
                                    "min_count": java_count,
                                }
                            )

                # Expected JAR
                expected.append(
                    {"path": f"{project_dir}/build/libs", "type": "jar", "artifact": "main JAR"}
                )

        return expected

    def _verify_expected_artifacts(
        self, project_dir: str, expected_artifacts: List[Dict[str, str]]
    ) -> Dict[str, any]:
        """
        Verify that expected artifacts actually exist.
        """
        # classes_expected / classes_found accumulate a SOURCE-WEIGHTED coverage:
        # each module contributes its source-file count, so building the large
        # modules counts more than the tiny ones. class_coverage (computed at the
        # end) lets validate_build_status accept a "most of the code compiled" build.
        result = {
            "all_present": True,
            "found": [],
            "missing": [],
            "classes_expected": 0,
            "classes_found": 0,
        }

        for expected in expected_artifacts:
            artifact_found = False

            if expected["type"] == "classes":
                min_expected = expected.get("min_count", 1)
                result["classes_expected"] += min_expected
                # For .class files, check if directory has sufficient class files
                class_count_cmd = (
                    f"find {expected['path']} -name '*.class' -type f 2>/dev/null | wc -l"
                )
                class_count_result = self._execute_command_with_logging(
                    class_count_cmd, f"counting classes in {expected['path']}"
                )

                if class_count_result["success"]:
                    class_count = int(class_count_result["output"].strip() or 0)
                    # Partial credit toward coverage (capped at the module's expectation).
                    result["classes_found"] += min(class_count, min_expected)

                    # We expect at least as many .class files as .java files
                    # (could be more due to inner classes, anonymous classes, etc.)
                    if class_count >= min_expected:
                        artifact_found = True
                        result["found"].append(
                            f"{expected['artifact']} ({class_count} classes found)"
                        )
                    else:
                        result["missing"].append(
                            f"{expected['artifact']} (found {class_count}, expected >={min_expected})"
                        )
                        result["all_present"] = False
                else:
                    result["missing"].append(expected["artifact"])
                    result["all_present"] = False

            elif expected["path"].endswith("/build/libs") or expected["path"].endswith("/target"):
                # Directory-based check for JARs (removed head limit to check all)
                check_cmd = (
                    f"find {expected['path']} -name '*.{expected['type']}' -type f 2>/dev/null"
                )
                check_result = self._execute_command_with_logging(
                    check_cmd, f"checking {expected['artifact']}"
                )

                if check_result["success"] and check_result.get("output", "").strip():
                    artifact_found = True
                    result["found"].append(expected["artifact"])
                else:
                    result["missing"].append(expected["artifact"])
                    result["all_present"] = False

            else:
                # Specific file check (Maven style)
                check_cmd = f"test -f {expected['path']} && echo EXISTS"
                check_result = self._execute_command_with_logging(
                    check_cmd, f"checking {expected['artifact']}"
                )

                if check_result["success"] and check_result.get("output", "").strip():
                    artifact_found = True
                    result["found"].append(expected["artifact"])
                else:
                    result["missing"].append(expected["artifact"])
                    result["all_present"] = False

        # Source-weighted fraction of expected classes actually produced —
        # stated ONLY when there was something to measure against. Plan 8 §3.4
        # (P2, "no basis is its own answer"): the old else-branch returned 1.0
        # for "nothing class-based was expected", and p7d polaris read that as
        # a met threshold — "Built 100% of expected classes" over a survey that
        # had derived no expectation at all. A basis of `none` carries NO
        # `class_coverage` key, so no caller can compare against a number
        # nothing produced.
        if result["classes_expected"] > 0:
            result["basis"] = "derived"
            result["class_coverage"] = result["classes_found"] / result["classes_expected"]
        else:
            result["basis"] = "none"
        return result

    def _validate_gradle_cache(self, project_dir: str) -> Dict[str, any]:
        """
        Validate a Gradle build by its REAL compiled outputs (not the cache dir).

        The PRIMARY (and only deciding) success criterion is the presence of
        compiled outputs produced by an actual build:
        - ``*.class`` files under any ``build/classes`` directory (covers the
          single-project ``build/classes/java/main`` layout and every
          subproject), and/or
        - real ``*.jar`` files under any ``build/libs`` directory.

        Wrapper/tooling jars (``gradle/wrapper/gradle-wrapper.jar``) are
        explicitly excluded - they ship with the repository and are not build
        outputs. The bare ``.gradle/`` cache directory is recorded only as a
        NON-DECIDING hint: its mere existence must never make a build "valid".
        """
        result = {"valid": False, "details": {}, "subprojects": []}

        # Non-deciding hint: the .gradle cache directory. Recorded for evidence
        # only; it does NOT contribute to validity.
        gradle_dir_cmd = f"test -d {project_dir}/.gradle"
        if self._execute_command_with_logging(gradle_dir_cmd, "checking .gradle cache (hint)")[
            "success"
        ]:
            result["details"]["gradle_cache_dir"] = True

        # PRIMARY: compiled .class files under any build/classes directory.
        class_cmd = (
            f"find {project_dir} -path '*/build/classes/*' -name '*.class' -type f "
            f"2>/dev/null | wc -l"
        )
        class_result = self._execute_command_with_logging(
            class_cmd, "counting Gradle compiled classes"
        )
        class_count = int(class_result["output"].strip() or 0) if class_result["success"] else 0
        if class_count > 0:
            result["details"]["class_count"] = class_count

        # PRIMARY: real JARs under any build/libs directory (exclude wrapper jar).
        jars_cmd = (
            f"find {project_dir} -path '*/build/libs/*.jar' -type f "
            f"-not -path '*/gradle/wrapper/*' 2>/dev/null | wc -l"
        )
        jars_result = self._execute_command_with_logging(jars_cmd, "counting Gradle JARs")
        jar_count = int(jars_result["output"].strip() or 0) if jars_result["success"] else 0
        if jar_count > 0:
            result["details"]["jar_count"] = jar_count

        # Informational: enumerate multi-project subprojects that actually
        # produced compiled output (does NOT decide validity on its own).
        subprojects_cmd = (
            f"find {project_dir} -mindepth 2 -maxdepth 2 "
            f"\\( -name 'build.gradle' -o -name 'build.gradle.kts' \\) 2>/dev/null"
        )
        subprojects_result = self._execute_command_with_logging(
            subprojects_cmd, "finding Gradle subprojects"
        )
        if subprojects_result["success"] and subprojects_result["output"]:
            subprojects = subprojects_result["output"].strip().split("\n")
            for subproject_build in subprojects:
                if subproject_build:
                    subproject_dir = "/".join(subproject_build.split("/")[:-1])
                    subproject_name = subproject_dir.split("/")[-1]

                    sub_class_cmd = (
                        f"find {subproject_dir} -path '*/build/classes/*' -name '*.class' "
                        f"-type f 2>/dev/null | wc -l"
                    )
                    sub_class_result = self._execute_command_with_logging(
                        sub_class_cmd, f"counting subproject {subproject_name} classes"
                    )
                    sub_class_count = (
                        int(sub_class_result["output"].strip() or 0)
                        if sub_class_result["success"]
                        else 0
                    )
                    if sub_class_count > 0:
                        result["subprojects"].append(subproject_name)

        # Determine validity from REAL compiled outputs only. The .gradle cache
        # directory alone can NEVER yield success.
        result["valid"] = class_count > 0 or jar_count > 0

        return result

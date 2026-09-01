"""Gradle tool with comprehensive error handling and Gradle-specific features."""

import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from loguru import logger

from sag.agent.evidence_assessments import ReceiptAssessment, write_assessment
from sag.agent.invocation_contracts import (
    CONTRACT_AUTHORITY_MISSING,
    contract_receipt_fields,
    current_contract,
    dispatch_contract,
    ensure_dispatch_contract,
)
from sag.agent.invocation_receipts import (
    DECLARED_OMISSION_REASONS,
    GRADLE_DISCOVERY_INCOMPLETE,
    GRADLE_NO_CLAIMED_TEST_REPORTS,
    GRADLE_NO_TEST_REPORTS,
    GRADLE_REPORTS_REWRITTEN,
    GRADLE_ROW_SAMPLE_UNREADABLE,
    GRADLE_SUITE_TOTALS_UNREADABLE,
    TESTCASE_FILE_CAP,
    TESTCASE_OUTCOME_CAP,
    TESTCASE_TAG_CAP,
    assemble_gradle_test_rows,
    parse_report_tag_rows,
    record_invocation,
    report_delta,
    report_tag_command,
    snapshot_reports,
)
from sag.agent.job_obligations import record_dispatch_obligation_result
from sag.agent.output_storage import OutputStorageManager
from sag.agent.receipt_test_rows import diagnostic_testcase_outcomes
from sag.evidence import EvidenceAssessment, TestStats
from sag.runtime.container_io import resolve_control_execute
from sag.utils.container_io import write_container_text_atomic

from ..base import BaseTool, ToolError, ToolResult
from .build_preflight import (
    JdkPreflight,
    active_java_major,
    classify_version_error,
    read_live_build_requirements,
)
from .build_utils import (
    DETACHED_HANDOFF_STATUSES,
    bounded_detached_log_excerpt,
    classify_detached_completion,
    detached_handoff_tool_result,
    detached_poll_ref,
    dispatch_hold_policy,
    harvest_detached_evidence,
)
from .dispatch_argv import bare_gradle_task, gradle_task_tokens
from .toolchain_manager import ToolchainManager, ToolchainSpec

# Gradle prints no reactor summary; what it prints is per-task outcomes:
#   > Task :camel-core:compileJava
#   > Task :camel-jms:compileJava NO-SOURCE
#   > Task :camel-ftp:test FAILED
# The project path in front of the task name is the module. Gradle states no
# per-module verdict, so this records only what it can prove: the module was
# attempted, and whether any of its tasks failed outright. `no-source` is an
# outcome of a task, not of a module, and is deliberately not a module status.
_GRADLE_TASK_ROW = re.compile(
    r"^>\s*Task\s+:?([A-Za-z0-9_.:-]*?):([A-Za-z0-9_]+)"
    r"(?:\s+(FAILED|NO-SOURCE|SKIPPED|UP-TO-DATE|FROM-CACHE))?\s*$",
    re.MULTILINE,
)
# Outcomes where Gradle guarantees the task's outputs match THIS build's
# inputs without rewriting them. Live kafka: `--build-cache` served most test
# tasks FROM-CACHE, their report files were never touched, the content hash
# did not move, and 4,686 passing tests could not be claimed by any receipt —
# they sat in auxiliary while the main count read 546. A cache hit is a
# stronger statement than "a file exists on disk": the build system vouches
# that the report on disk IS this build's result for that task.
_GRADLE_CURRENT_WITHOUT_REWRITE = ("FROM-CACHE", "UP-TO-DATE")
# The task whose outputs are test reports. Only its cache hits may claim one.
_GRADLE_TEST_TASKS = ("test", "integrationTest", "check")


def _gradle_cached_report_dirs(output: str, working_directory: str) -> List[str]:
    """Report directories a cached/up-to-date TEST task vouches for.

    Only test tasks, because only they produce test reports; a cached
    `compileJava` says nothing about any report. The directory is Gradle's own
    layout for the module the task belongs to.
    """
    root = str(working_directory or "").rstrip("/")
    if not root:
        return []
    dirs: List[str] = []
    for path, task, outcome in _GRADLE_TASK_ROW.findall(str(output or "")):
        if (outcome or "").upper() not in _GRADLE_CURRENT_WITHOUT_REWRITE:
            continue
        if task not in _GRADLE_TEST_TASKS:
            continue
        module_path = path.strip(":").replace(":", "/").strip()
        base = f"{root}/{module_path}" if module_path else root
        candidate = f"{base}/build/test-results/{task}"
        if candidate not in dirs:
            dirs.append(candidate)
    return dirs


def _gradle_module_outcomes(output: str) -> List[Dict[str, str]]:
    """`[{module, status}]` for every module whose tasks this build ran.

    `status` is "failure" when any of that module's tasks FAILED, else
    "attempted" — never "success", because a task list is not a statement
    that the module built correctly, and inventing that verdict here is
    exactly the overclaim the physical check exists to catch.
    """
    statuses: Dict[str, str] = {}
    order: List[str] = []
    for path, _task, outcome in _GRADLE_TASK_ROW.findall(str(output or "")):
        module = path.strip(":").strip()
        if not module:
            module = ":root"
        if module not in statuses:
            statuses[module] = "attempted"
            order.append(module)
        if (outcome or "").upper() == "FAILED":
            statuses[module] = "failure"
    return [{"module": module, "status": statuses[module]} for module in order]


# --- Post-dispatch test-report harvest -------------------------------------
#
# Grounded in a measured study of three Gradle projects' own containers
# (docs/superpowers/reports/gradle-evidence-20260830.md). The kafka run this
# exists for left 1,176 report files and 27,219 executed tests on disk and its
# receipt reported none of it.
#
# Two tiers, because one read cannot do both jobs. Tier 1 sums the
# `<testsuite>` root attributes of every report THIS INVOCATION CLAIMS, which
# is the COMPLETE count for it and costs a fixed few kilobytes per file. Tier 2
# samples per-testcase identities from the same claim set under the existing tag
# bounds, red-bearing reports first, and states what it dropped. Neither tier
# ever reads a report whole: kafka's largest single XML is 137.8 MB against a
# 16 MB receipt canonical budget, so a whole-file read is impossible by
# construction, not merely expensive.
#
# Both tiers answer to the receipt's own `report_delta`, and the scan is wider
# than that on purpose — the tree holds every earlier attempt's leftovers too.
# What discovery proves is presence and absence; what the delta decides is
# whose. A count summed off a file the delta refused to claim would be another
# dispatch's tests wearing this receipt's identity.

# Reports discovery will hand on. kafka measured 1,176 and geode 1,167, so this
# is a safety bound rather than an expected one; a discovery that hits it says
# how many reports it left unsummed.
#
# It bounds a LISTING, and since r2-T2 a listing decides nothing about this
# receipt's evidence: the claim set is enumerated from `report_delta` itself.
# What this bound can still cost is the unclaimed-presence statement discovery
# exists to make — never a claimed report's counts.
GRADLE_XML_FILE_CAP = 2048
# Claimed reports one tier-1 round trip may cover. The claim set is the delta's
# now, and the delta is as large as the dispatch's own output (kafka: 1,176),
# so the read needs its OWN bound — 4,096 entries of `{path, sha256, 4 counts}`
# is roughly 800 KB of JSON, against a measured worst case under a third of it.
# A claim this bound leaves out is disclosed as `unsummarized_files`, which
# withdraws red-completeness: a red can hide in a report nobody summed.
GRADLE_CLAIMED_FILE_CAP = 4096
# A JUnit report's root `<testsuite>` tag is its first element, on the second
# line after the XML declaration. 4 KB is many times what that tag needs and is
# the same read whether the file is 438 bytes or 137.8 MB.
GRADLE_SUITE_HEAD_BYTES = 4096
_GRADLE_REPORT_COUNT_MARKER = "###sag-gradle-xml-count###"
# `find`'s OWN exit status, carried in band. `2>/dev/null` throws its errors
# away and the pipeline's status belongs to `awk`, so without this line a find
# that never read the tree — a workdir that vanished, a subtree it could not
# descend — is indistinguishable from one that read it all and found nothing.
# The scan has to state that it finished; the marker alone only states that
# `awk` did.
_GRADLE_FIND_STATUS_MARKER = "###sag-gradle-find-status###"
# Gradle writes reports per TASK: `build/test-results/<task>/TEST-*.xml`. geode
# writes `distributedTest` beside `test`, so the task dir is discovered, never
# assumed. `binary/` under it is Gradle's own internal result store and holds
# no report this engine may read.
_GRADLE_REPORT_GLOB = "*/build/test-results/*/*.xml"
_GRADLE_REPORT_MARKER = "/build/test-results/"
_GRADLE_BINARY_GLOB = "*/binary/*"
# The same exclusion, applied to the claim set: `report_delta`'s snapshot globs
# `*/build/test-results/*.xml` without excluding Gradle's internal result store,
# so the rule that keeps `binary/` away from a report parser has to hold on the
# path discovery no longer gates.
_GRADLE_BINARY_MARKER = "/binary/"
# A digest a delta actually recorded, not whatever a broken snapshot echoed.
_GRADLE_SHA256_RE = re.compile(r"[0-9a-f]{64}")
# Read the FIRST `<testsuite ...>` open tag out of a bounded head and take the
# four counts it declares. `\b` is what keeps `<testsuites>` from matching: a
# wrapper element states no counts of its own. A head that yields no such tag
# is counted as unreadable and never guessed at.
#
# The SAME open that yields the head also hashes the file WHOLE (P-B: every
# count is bound to the bytes it counts). One descriptor, one forward pass: the
# head is the first chunk of the stream the digest covers, so no window exists
# in which the counts and the digest could describe different bytes — which is
# exactly what a report rewritten by a concurrent dispatch under the same
# workdir would produce. Hashing costs one sequential read of bytes the
# invocation's own hash bracket already reads twice; it never buffers a report,
# so kafka's 137.8 MB stream costs 137.8 MB of I/O and 4 KB of memory.
_GRADLE_SUITE_HEAD_READER = r"""
import hashlib
import json
import re
import sys

SUITE = re.compile(rb"<testsuite\b[^>]*>")
ATTRIBUTE = re.compile(rb'([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*"([^"]*)"')
FIELDS = (b"tests", b"failures", b"errors", b"skipped")
CHUNK = 1 << 20

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        paths = json.load(handle)
except Exception:
    print(json.dumps({"status": "unavailable", "suites": []}))
    raise SystemExit(0)

suites = []
for path in paths:
    entry = {"path": path}
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            head = handle.read(HEAD_BYTES)
            digest.update(head)
            while True:
                chunk = handle.read(CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        entry["sha256"] = digest.hexdigest()
    except Exception:
        suites.append({"path": path})
        continue
    match = SUITE.search(head)
    if match is not None:
        attributes = dict(ATTRIBUTE.findall(match.group(0)))
        for field in FIELDS:
            try:
                value = int(attributes[field])
            except (KeyError, TypeError, ValueError):
                continue
            if value >= 0:
                entry[field.decode()] = value
    suites.append(entry)

print(json.dumps({"status": "complete", "suites": suites}, separators=(",", ":")))
"""
_GRADLE_HEAD_INPUT_DIR = "/workspace/.setup_agent/.gradle-suite-head-input"
# The task names whose reports are test evidence. A dispatch that ran none of
# them harvests nothing and states nothing: `assemble` leaving no test XML is
# not missing evidence, it is a build.
_GRADLE_TEST_ACTIONS = frozenset(
    {"test", "check", "integrationtest", "functionaltest", "distributedtest", "build"}
)


class GradleReportDiscovery(NamedTuple):
    """What is on disk under the build's own report layout.

    `complete` is the load-bearing bit. A discovery that did not run to its
    marker knows nothing, and "no reports found" from an unfinished scan would
    be the exact lie the ofbiz case exists to forbid — absence is only
    claimable when the scan proved it.
    """

    paths: Tuple[str, ...]
    total: int
    complete: bool


class GradleTestHarvest(NamedTuple):
    """One dispatch's test evidence, in the shapes the receipt carries."""

    suite_summaries: Optional[Dict[str, Any]] = None
    row_disclosure: Optional[Dict[str, Any]] = None
    testcase_outcomes: Optional[Dict[str, Any]] = None
    module_tests_reported: Mapping[str, int] = {}
    omissions: Tuple[Dict[str, Any], ...] = ()


def gradle_test_action(*actions: Any) -> bool:
    """Whether any of these action strings names Gradle test work."""

    for action in actions:
        for token in gradle_task_tokens(action) or ():
            if bare_gradle_task(token).lower() in _GRADLE_TEST_ACTIONS:
                return True
    return False


def _gradle_report_identity(path: str, working_directory: str) -> Optional[tuple]:
    """`(project path, task dir)` for one report, or None when unprovable.

    The project path is Gradle's own (`:root`, `:clients`, `:connect:api`) and
    is derived from the report's location under the build root — the same
    boundary `receipt_test_rows._report_module_root` uses. A report outside the
    dispatch's own working directory belongs to no project this dispatch ran.
    """

    text = str(path or "").strip()
    prefix, separator, tail = text.partition(_GRADLE_REPORT_MARKER)
    if not separator or "/" not in tail:
        return None
    task = tail.split("/", 1)[0].strip()
    root = str(working_directory or "").rstrip("/")
    if not task or not root:
        return None
    if prefix != root and not prefix.startswith(root + "/"):
        return None
    relative = prefix[len(root) :].strip("/")
    return (":" + relative.replace("/", ":") if relative else ":root"), task


def _gradle_module_outcome_key(project_path: str) -> str:
    """The project path as `_gradle_module_outcomes` spells it.

    That parser strips the leading colon off every path it reads from the task
    stream and writes the root project as `:root`. Merging counts into its list
    has to speak its grammar, not a second one.
    """

    coordinate = str(project_path or "").strip()
    return coordinate if coordinate == ":root" else coordinate.lstrip(":")


def _gradle_discover_reports(execute, working_directory: str) -> GradleReportDiscovery:
    """Every test report on disk under this build root, in ONE round trip.

    `awk` prints the bounded path list and then the TRUE total, so a bound that
    fired states exactly how many reports it left behind instead of silently
    handing back a short list.

    Completeness is `find`'s to state, not `awk`'s. The END block runs whatever
    happened upstream — over a partial list, over no list at all — so a marker
    line proves only that the last stage of the pipeline ran. `find` therefore
    prints its OWN exit status into the stream, and a scan whose `find` did not
    return 0 is an unfinished scan: it proves nothing about absence, and
    "no reports found" from a directory it never read is the exact false
    absence claim `GradleReportDiscovery` exists to forbid.
    """

    root = str(working_directory or "").strip().rstrip("/")
    if not root:
        return GradleReportDiscovery((), 0, False)
    program = (
        f'index($0, "{_GRADLE_FIND_STATUS_MARKER}") == 1 '
        f"{{ status = substr($0, {len(_GRADLE_FIND_STATUS_MARKER) + 1}); next }} "
        f"count < {GRADLE_XML_FILE_CAP} {{ print }} "
        f"{{ count++ }} "
        f'END {{ printf "%s%d %s\\n", "{_GRADLE_REPORT_COUNT_MARKER}", count, status }}'
    )
    command = (
        f"{{ find {shlex.quote(root)} -type f -path {shlex.quote(_GRADLE_REPORT_GLOB)} "
        f"! -path {shlex.quote(_GRADLE_BINARY_GLOB)} 2>/dev/null; "
        f"printf '%s%d\\n' {shlex.quote(_GRADLE_FIND_STATUS_MARKER)} \"$?\"; }} "
        f"| LC_ALL=C sort | awk {shlex.quote(program)}"
    )
    output = _gradle_untruncated_output(execute, command)
    if output is None:
        return GradleReportDiscovery((), 0, False)
    paths: List[str] = []
    total: Optional[int] = None
    scan_status: Optional[str] = None
    for line in output.splitlines():
        line = line.strip()
        if line.startswith(_GRADLE_REPORT_COUNT_MARKER):
            counted, _, status = line[len(_GRADLE_REPORT_COUNT_MARKER) :].partition(" ")
            try:
                total = int(counted)
            except ValueError:
                total = None
            scan_status = status.strip()
            continue
        if line.startswith("/"):
            paths.append(line)
    if total is None or scan_status != "0" or total < len(paths):
        return GradleReportDiscovery((), 0, False)
    return GradleReportDiscovery(tuple(paths), total, True)


def _gradle_untruncated_output(execute, command: str) -> Optional[str]:
    """One evidence read on the MACHINE path, or None when it did not complete.

    The presentation path clamps output at ~10,000 characters; 1,176 report
    paths is far past that, and a clamped list would decide the harvest's claim
    set instead of the run doing it. The TypeError fallback keeps narrow test
    doubles working, exactly as `snapshot_reports` does.

    Unlike `snapshot_reports` this accepts the ordinary executor when no clean
    control executor is exposed, because nothing read here can launder a claim:
    the receipt's claim set is the hash bracket's delta, and an identity row
    still has to match a digest that bracket recorded before it counts.
    """

    control = resolve_control_execute(execute) or execute
    if not callable(control):
        return None
    try:
        try:
            result = control(command, truncate_output=False) or {}
        except TypeError as exc:
            if "truncate_output" not in str(exc):
                raise
            result = control(command) or {}
    except Exception as exc:  # evidence collection never breaks the runner
        logger.debug(f"gradle report harvest read skipped: {exc}")
        return None
    if (
        not isinstance(result, Mapping)
        or result.get("success") is False
        or result.get("dispatch_status")
        or result.get("exit_code") not in (None, 0)
    ):
        return None
    return str(result.get("output") or "")


def _gradle_suite_head_entries(
    execute,
    paths: Sequence[str],
    working_directory: str,
) -> Optional[List[Dict[str, Any]]]:
    """One `{module, task, sha256, tests, failures, errors, skipped}` per report.

    The path list is written into the container and passed by ONE argument, so
    a reactor with thousands of reports can never approach ARG_MAX — the same
    transport `receipt_test_rows.read_delta_testcase_rows` uses, and for the
    same reason. Only the four declared counts and the file's digest come back;
    report bodies never cross the boundary and never reach model-visible output.

    The digest is of the WHOLE file and is taken in the same read as the head,
    so a caller can bind every count to the bytes it counted (P-B). It is the
    reader's statement about what it read, not a claim about whose bytes those
    are: `gradle_test_harvest` compares it against the delta.

    Returns None when the transport itself did not complete. A report whose
    head carried no parsable `<testsuite>` root comes back WITHOUT counts, and
    the fold counts it as unreadable rather than summing a zero into a total.
    """

    if not paths:
        return []
    body = json.dumps(list(paths), ensure_ascii=False, separators=(",", ":"))
    # Named for its own content, not for a process-salted `hash()`: two
    # concurrent harvests of different trees cannot collide, and re-running the
    # same one is idempotent.
    slug = hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]
    input_path = f"{_GRADLE_HEAD_INPUT_DIR}/{slug}.json"
    if not write_container_text_atomic(execute, input_path, body, validate_json=True).persisted:
        return None
    script = _GRADLE_SUITE_HEAD_READER.replace("HEAD_BYTES", str(GRADLE_SUITE_HEAD_BYTES))
    command = f"python3 -c {shlex.quote(script)} {shlex.quote(input_path)}"
    try:
        output = _gradle_untruncated_output(execute, command)
    finally:
        try:
            execute(f"rm -f -- {shlex.quote(input_path)}")
        except Exception:
            pass
    if output is None:
        return None
    try:
        payload = json.loads(output)
    except (TypeError, ValueError) as exc:
        logger.debug(f"gradle suite totals unreadable: {exc}")
        return None
    if not isinstance(payload, Mapping) or payload.get("status") != "complete":
        return None
    suites = payload.get("suites")
    if not isinstance(suites, list):
        return None
    entries: List[Dict[str, Any]] = []
    for suite in suites:
        if not isinstance(suite, Mapping):
            continue
        identity = _gradle_report_identity(str(suite.get("path") or ""), working_directory)
        if identity is None:
            continue
        module, task = identity
        entry: Dict[str, Any] = {"module": module, "task": task, "path": suite.get("path")}
        digest = str(suite.get("sha256") or "").strip().lower()
        if _GRADLE_SHA256_RE.fullmatch(digest):
            entry["sha256"] = digest
        for field in ("tests", "failures", "errors", "skipped"):
            if field in suite:
                entry[field] = suite.get(field)
        entries.append(entry)
    return entries


def _gradle_claimed_report_paths(
    claims: Mapping[str, str],
    working_directory: str,
) -> Tuple[str, ...]:
    """Every report path the DELTA claims, in one deterministic order.

    The claim set is the delta's own path list and nothing else. It was gated
    through the discovery listing until r2-T2, which meant a claimed report the
    2,048-entry bound never named could not be summed — and, when the bound cut
    every one of them, the harvest declared `gradle_no_claimed_test_reports`
    over reports this dispatch demonstrably wrote. A listing bound may cost a
    receipt an unclaimed-presence remark; it may never cost it its evidence.

    A claim is a report of this dispatch's when it carries a real digest, sits
    under this build root's `test-results/<taskdir>/` layout, and is not
    Gradle's internal `binary/` store — the same three rules discovery applied,
    now applied where the claim is made.
    """

    return tuple(
        sorted(
            path
            for path, digest in (claims or {}).items()
            if _GRADLE_SHA256_RE.fullmatch(str(digest or ""))
            and _GRADLE_BINARY_MARKER not in path
            and _gradle_report_identity(path, working_directory) is not None
        )
    )


def _gradle_claim_bound_entries(
    entries: Sequence[Mapping[str, Any]],
    claims: Mapping[str, str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The head entries whose bytes are still the delta's, and the ones that moved.

    P-B, at the one place tier-1 could break it: the digest comes back from the
    same read as the counts, so a report rewritten between the invocation's
    `after` snapshot and this read is caught here rather than summed. That is
    the same-workdir concurrency defense — a second dispatch writing under the
    same tree while this one harvests — and it is also the plain rewrite case a
    long-running reactor produces on its own.

    An excluded file contributes to NOTHING: not the totals, not the module
    witnesses, not the identity sample. It is disclosed instead, by count and
    by a bounded path sample, so the receipt says which measurement it withheld
    and why. A file whose read yielded no digest at all is not a rewrite claim
    — it is an unreadable report, and it is passed through without counts so
    the fold records it as one.
    """

    kept: List[Dict[str, Any]] = []
    rewritten: List[str] = []
    for entry in entries or ():
        if not isinstance(entry, Mapping):
            continue
        path = str(entry.get("path") or "")
        claimed = str((claims or {}).get(path) or "").lower()
        digest = str(entry.get("sha256") or "").lower()
        if not claimed:
            # Never asked for; a reader that answered anyway states nothing here.
            continue
        if not digest:
            kept.append(
                {field: entry[field] for field in ("module", "task", "path") if field in entry}
            )
            continue
        if digest != claimed:
            rewritten.append(path)
            continue
        kept.append(dict(entry))
    return kept, sorted(rewritten)


def _gradle_red_first(entries: Sequence[Mapping[str, Any]]) -> List[str]:
    """Report paths, every red-bearing one before every other.

    kafka's 8 reds live among 27,219 tests in 1,176 reports. Any bound applied
    to a list that is not ordered this way loses them, and a run that cannot
    name its failures has reported nothing a repair can act on.
    """

    def rank(entry: Mapping[str, Any]) -> tuple:
        failures = entry.get("failures")
        errors = entry.get("errors")
        red = (failures if isinstance(failures, int) else 0) + (
            errors if isinstance(errors, int) else 0
        )
        return (0 if red > 0 else 1, str(entry.get("path") or ""))

    return [str(entry.get("path") or "") for entry in sorted(entries, key=rank)]


def _gradle_identity_rows(
    execute,
    paths: Sequence[str],
    claims: Mapping[str, str],
) -> Optional[List[Dict[str, Any]]]:
    """Bounded per-testcase identities from the reports this receipt claims.

    Tag tokens only, `TESTCASE_TAG_CAP` per report — the maven-side bound,
    unchanged; the caller has already applied `TESTCASE_FILE_CAP` so the file
    set this read covers and the file set the disclosure discloses are one
    list. Each report's tokens are headed by the digest of the file they were
    read from, and a digest the receipt's report delta does not claim
    contributes nothing: an identity is only this invocation's when the bytes
    behind it are.

    `None` is the round trip that did not complete, and it is a different fact
    from `[]` (it ran and the claimed bytes yielded no identity). Only the
    second is a sample; the first is no sample at all, and a disclosure written
    over it would describe rows the receipt does not carry.
    """

    if not paths:
        return []
    command = "; ".join(report_tag_command(path, tag_cap=TESTCASE_TAG_CAP) for path in paths)
    output = _gradle_untruncated_output(execute, command)
    if output is None:
        return None
    return parse_report_tag_rows(output, report_claims=claims)


def gradle_test_harvest(
    execute,
    *,
    working_directory: str,
    delta: Mapping[str, Any],
    test_dispatch: bool,
) -> GradleTestHarvest:
    """Harvest one terminal Gradle test dispatch's report evidence.

    Derivable from `(execute, working_directory, delta, action)` alone, which
    is exactly what a detached job holds at settlement — so the settled path
    states what the synchronous one does instead of quietly lacking it.

    Reports this invocation CLAIMS, read and summed, become totals, and a bound
    that fired becomes a recorded drop. WHOSE the reports are is the delta's
    answer and only the delta's: it names every path this dispatch wrote or the
    build system vouched for, with the digest each was written at, so the claim
    set is enumerated straight from it. A tree scan cannot add to that set and
    since r2-T2 cannot subtract from it either.

    Discovery has one job left, and it is the one only a scan can do: state
    what is on disk when the delta claims NOTHING. A scan that never finished
    proved neither presence nor absence and states nothing — unknown is an
    absent key here as it is for every other v2 fact. A scan that finished is
    proof: a tree holding reports of which none is this dispatch's, and a test
    run that left none at all, are different recorded omissions. Everything the
    delta does claim is measured without asking the tree at all.

    The one thing this never returns is a zero.
    """

    if not test_dispatch:
        return GradleTestHarvest()
    claims = {
        str((entry or {}).get("path") or ""): str((entry or {}).get("sha256") or "").lower()
        for bucket in ("new", "changed", "cached")
        for entry in (delta or {}).get(bucket) or ()
        if isinstance(entry, Mapping)
    }
    # THE binding, and it is the same one the identity rows already answered to.
    # `report_delta` puts a byte-identical leftover from an earlier dispatch in
    # no bucket, so a claim is a report this invocation wrote or the build
    # system vouched for — never one it merely found. A repair loop re-running
    # `:clients:test` over a full reactor's leftovers is the ordinary case, not
    # the exotic one, and summing what the tree holds would state 9,018 of
    # another dispatch's tests as this receipt's.
    claimed = _gradle_claimed_report_paths(claims, working_directory)
    if not claimed:
        return _gradle_unclaimed_presence(execute, working_directory)
    # The tier-1 read's own bound, and the only one that can now cost this
    # receipt a claimed report's counts. Sorted, so which claims a fired bound
    # leaves out is a property of the paths and not of dict order.
    read = claimed[:GRADLE_CLAIMED_FILE_CAP]
    unsummarized = len(claimed) - len(read)
    entries = _gradle_suite_head_entries(execute, read, working_directory)
    if entries is None:
        return GradleTestHarvest(omissions=_gradle_omissions(GRADLE_SUITE_TOTALS_UNREADABLE))
    # P-B: a count is summed only when the digest read beside it is the digest
    # the delta claims. Everything downstream — totals, module witnesses, the
    # identity sample's file list — runs off the bound entries alone.
    entries, rewritten = _gradle_claim_bound_entries(entries, claims)
    # A claimed report the read answered for NEITHER way — no counts, no digest,
    # no line at all — is not a zero and not a silent subtraction: it is an
    # unreadable report, and the fold counts it as one. Without this a reader
    # that returned a short list would quietly shrink the claim set.
    answered = {str(entry.get("path") or "") for entry in entries} | set(rewritten)
    entries.extend({"path": path} for path in read if path not in answered)
    if not entries and rewritten:
        # Every claimed report moved under the read. There is no total to state
        # and no section to hang the disclosure on, so the receipt names the
        # measurement it lost and the reason it lost it.
        return GradleTestHarvest(omissions=_gradle_omissions(GRADLE_REPORTS_REWRITTEN))
    tests_reported: Dict[str, int] = {}
    for entry in entries:
        total = entry.get("tests")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            continue
        key = _gradle_module_outcome_key(str(entry.get("module") or ""))
        if key:
            tests_reported[key] = tests_reported.get(key, 0) + total
    ordered = _gradle_red_first(entries)
    sampled = ordered[:TESTCASE_FILE_CAP]
    rows = _gradle_identity_rows(execute, sampled, claims)
    summaries, kept_rows, disclosure = assemble_gradle_test_rows(
        entries,
        rows or [],
        # The identity sample IS the receipt's bounded diagnostic list, so it is
        # bounded by that list's own cap; disclosing drops against a larger one
        # would describe rows the receipt does not carry.
        row_cap=TESTCASE_OUTCOME_CAP,
        harvested_files=ordered if rows is not None else (),
        unsummarized_files=unsummarized,
        rewritten_files=rewritten,
    )
    if summaries is None:
        return GradleTestHarvest(omissions=_gradle_omissions(GRADLE_SUITE_TOTALS_UNREADABLE))
    if rows is None:
        # The totals stand — a different read produced them — but there is no
        # sample, so nothing may be disclosed about one. The receipt states the
        # counts, names the measurement it could not make, and leaves its
        # bounded list to the transports below.
        return GradleTestHarvest(
            suite_summaries=summaries,
            module_tests_reported=tests_reported,
            omissions=_gradle_omissions(
                GRADLE_ROW_SAMPLE_UNREADABLE, fields=("gradle_row_disclosure",)
            ),
        )
    outcomes = diagnostic_testcase_outcomes(
        {
            # `diagnostic_testcase_outcomes` marks the list truncated for any
            # status but "complete", which is exactly right here: a sample that
            # dropped anything is a sample, and the disclosure beside it says
            # by how much.
            "status": "complete" if not (disclosure or {}).get("rows_truncated") else "bounded",
            "rows": kept_rows,
        }
    )
    return GradleTestHarvest(
        suite_summaries=summaries,
        row_disclosure=disclosure,
        testcase_outcomes=outcomes,
        module_tests_reported=tests_reported,
    )


def _gradle_unclaimed_presence(execute, working_directory: str) -> GradleTestHarvest:
    """What the TREE says when the delta claims nothing — discovery's one job.

    Absence is the only fact a scan can establish that a delta cannot, and it
    is two facts, not one: a tree that holds no report at all (ofbiz-plugins ran
    its test task and wrote no XML) and a tree whose every report belongs to an
    earlier dispatch. Both are omissions; neither is a zero.

    `gradle_no_claimed_test_reports` is reachable from HERE and nowhere else,
    which is the point: it now states what the delta proves — that this
    dispatch wrote nothing — and never what a 2,048-entry listing failed to
    name. A scan that did not finish proves neither presence nor absence and
    declares nothing at all (see `_gradle_omissions`).
    """

    discovery = _gradle_discover_reports(execute, working_directory)
    if not discovery.complete:
        logger.debug(f"{GRADLE_DISCOVERY_INCOMPLETE} under {working_directory}")
        return GradleTestHarvest()
    if not discovery.total:
        return GradleTestHarvest(omissions=_gradle_omissions(GRADLE_NO_TEST_REPORTS))
    return GradleTestHarvest(omissions=_gradle_omissions(GRADLE_NO_CLAIMED_TEST_REPORTS))


def _gradle_omissions(
    reason: str,
    *,
    fields: Sequence[str] = ("gradle_suite_summaries", "gradle_row_disclosure"),
) -> Tuple[Dict[str, Any], ...]:
    """The named harvested sections, each stated unavailable for one reason.

    An omission is a claim that something IS missing, so only a scan that ran
    to completion may make one. `GRADLE_DISCOVERY_INCOMPLETE` is therefore not
    in the vocabulary at all: a receipt that could not look has nothing to
    declare, and declaring anyway would turn every unanswered probe into
    evidence of a barren build.

    Both sections fail together for every reason that stops the harvest before
    it has counts. `fields` is for the one that does not: a totals read that
    landed and a tag read that did not leaves the summaries standing and only
    the row disclosure missing.
    """

    if reason not in DECLARED_OMISSION_REASONS:
        return ()
    return tuple({"field": field, "reasons": [reason]} for field in fields)


def _gradle_module_outcomes_with_counts(
    module_outcomes: Sequence[Mapping[str, Any]],
    tests_reported: Mapping[str, int],
) -> List[Dict[str, Any]]:
    """The task stream's module list, with an executed witness where proved.

    Enrich only — never extend. `module_outcomes` is the coverage denominator,
    and a module that appears here solely because a report file survived from
    an earlier invocation would inflate it. A module Gradle did not name in
    THIS dispatch's task stream is not this dispatch's module.
    """

    enriched: List[Dict[str, Any]] = []
    for entry in module_outcomes or ():
        if not isinstance(entry, Mapping):
            continue
        row = dict(entry)
        total = tests_reported.get(str(row.get("module") or ""))
        if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
            row["tests_reported"] = total
        enriched.append(row)
    return enriched


class GradleTool(BaseTool):
    """Gradle build tool with enhanced error handling and Gradle-specific features."""

    def __init__(self, orchestrator, toolchain_manager: ToolchainManager = None):
        super().__init__(
            name="gradle",
            description="Execute Gradle commands with comprehensive error analysis and raw output access. "
            "Supports all Gradle tasks, multi-project builds, dependency management, and build analysis. "
            "Automatically uses gradlew wrapper if present, installs Gradle if needed.",
        )
        self.orchestrator = orchestrator
        self.toolchain_manager = toolchain_manager or ToolchainManager(orchestrator)
        self.output_storage = None  # Will be initialized when needed
        # Receipt facts for the LAST physical dispatch of the current execute()
        # call (P0-A); merged into whichever ToolResult that call returns.
        self._pending_invocation_receipt: Optional[Dict[str, Any]] = None

    def _extract_key_info(self, output: str, tool_name: str) -> str:
        """Override to use Gradle-specific extraction."""
        if tool_name == "gradle" or tool_name == self.name:
            return self._extract_gradle_key_info(output)
        return output

    def execute(
        self,
        tasks: str = None,
        command: str = None,  # Alias for tasks for compatibility
        properties: str = None,
        gradle_args: str = None,
        build_file: str = None,
        raw_output: bool = False,
        working_directory: str = "/workspace",
        timeout: int = 300,
        use_wrapper: bool = True,  # Gradle-specific: prefer wrapper
        parallel: bool = False,  # Gradle-specific: parallel execution
        configure_on_demand: bool = False,  # Gradle-specific optimization
        build_cache: bool = True,  # Gradle-specific: use build cache
        fail_at_end: bool = False,  # Continue execution after failures
        *,
        _env_preflight: bool = True,
        _compile_source_languages: Optional[List[str]] = None,
        _requirements: Optional[Mapping[str, Any]] = None,
    ) -> ToolResult:
        """
        Execute Gradle commands with comprehensive error handling.

        Args:
            tasks: Gradle tasks to run (e.g., 'clean build', 'test', 'assemble')
            command: Alias for tasks (for compatibility)
            properties: Gradle properties (e.g., '-Pversion=1.0', '-PskipTests')
            gradle_args: Additional Gradle arguments (e.g., '--info', '--stacktrace', '--scan')
            build_file: Specific build file to use (e.g., 'custom.gradle', 'build.gradle.kts')
            raw_output: Whether to return raw Gradle output for detailed analysis
            working_directory: Directory to execute Gradle in
            timeout: Command timeout in seconds
            use_wrapper: Whether to prefer gradlew wrapper over system gradle
            parallel: Enable parallel execution
            configure_on_demand: Only configure relevant projects
            build_cache: Use Gradle build cache for faster builds
            fail_at_end: IMPORTANT for multi-module projects! Set to True to continue after task failures.
                        For test tasks, automatically adds test.ignoreFailures=true.
                        Essential for running ALL tests in multi-module projects.

            _compile_source_languages: compile languages whose src/main/<lang>
                        directories the caller PROBED before choosing these
                        tasks. A gradle build that reports NO-SOURCE for every
                        compile task it ran cannot be scored as a compile of
                        those sources (see _compile_coverage_error).
        """

        self._pending_invocation_receipt = None
        self._pending_log_storage_metadata: Dict[str, Any] = {}
        if current_contract() is None:
            return ToolResult.completed_failure(
                output="[contract] Gradle was not dispatched: no facade-frozen contract is active.",
                error="invocation contract authority missing",
                error_code=CONTRACT_AUTHORITY_MISSING,
                metadata={"runner_dispatched": False, "tool": "gradle"},
            )

        # Handle command as alias for tasks
        if command and not tasks:
            tasks = command

        # Whether the agent explicitly scoped this invocation. The
        # orchestration layer (PR #12) owns working-directory injection, so
        # this never re-targets; explicitness only gates the [scope] warning.
        explicitly_scoped = working_directory not in (None, "/workspace")

        # --- JDK pre-flight (spec §1b): check-and-fix, never a hard block ---
        # Single-ownership rule: the consolidated build facade (BuildTool)
        # runs the pre-flight, the bounded retry and the [scope] narration on
        # its path and passes _env_preflight=False; only callers that invoke the
        # internal tool directly keep the guarantee here. Exactly one
        # layer probes the container — and reruns — per build.
        preamble_lines: List[str] = []
        outcome = None
        # The manifest the pre-flight already read, kept for the invocation
        # receipt's survey pins and domain. The facade path supplies the exact
        # host-verified revision it already read so receipt sealing does not
        # re-read the manifest or discard test-module coordinates.
        requirements: Dict[str, Any] = dict(_requirements or {})
        if _env_preflight:
            manifest_read = read_live_build_requirements(self.orchestrator)
            if (
                not manifest_read.complete
                or manifest_read.conflict is not None
                or manifest_read.payload is None
            ):
                return ToolResult.completed_failure(
                    output=(
                        "[evidence] Gradle was not dispatched: build requirements are "
                        "not a complete current host-published revision."
                    ),
                    error="live build requirements unavailable",
                    error_code="BUILD_REQUIREMENTS_UNAVAILABLE",
                    facts={
                        "working_directory": working_directory,
                        "build_requirements_status": manifest_read.conflict or "absent",
                    },
                    metadata={
                        "runner_dispatched": False,
                        "tool": "gradle",
                        "blocker_owner": "harness",
                    },
                )
            requirements = dict(manifest_read.payload)
            outcome = JdkPreflight(self.orchestrator).run(
                requirements.get("java_version"),
                source=requirements.get("java_version_source") or "unknown",
            )
            if outcome.narration:
                preamble_lines.append(outcome.narration)

            # [scope] warning ONLY for explicit narrowing (spec §3), with the
            # facade's semantics: an explicit working_directory strictly
            # DEEPER than a healthy reactor's recommended build root.
            # Working-directory defaulting lives in the orchestration layer
            # (PR #12); the tool never re-targets and never mutates
            # fail_at_end on its own.
            recommended_root = (requirements.get("build_root") or "").rstrip("/")
            if (
                explicitly_scoped
                and requirements.get("root_shape") == "healthy_reactor"
                and recommended_root
                and (working_directory or "").rstrip("/").startswith(recommended_root + "/")
            ):
                preamble_lines.append(
                    "[scope] narrower than the recommended reactor root "
                    f"({recommended_root}) — sibling deps may be unresolved; "
                    "tests outside this module will not run"
                )
        preamble = ("\n".join(preamble_lines) + "\n") if preamble_lines else ""

        resolved_gradle = self._resolve_gradle_executable(
            working_directory=working_directory,
            prefer_wrapper=use_wrapper,
        )
        gradle_executable = self._determine_gradle_executable(
            working_directory,
            use_wrapper,
            resolved_gradle=resolved_gradle,
        )
        if not gradle_executable:
            install_result = self._install_gradle(working_directory)
            if not install_result.succeeded:
                return install_result
            resolved_gradle = self._resolve_gradle_executable(
                working_directory=working_directory,
                prefer_wrapper=use_wrapper,
            )
            gradle_executable = self._determine_gradle_executable(
                working_directory,
                use_wrapper,
                resolved_gradle=resolved_gradle,
            )
            if not gradle_executable:
                return ToolResult.completed_failure(
                    output="",
                    error="No Gradle executable could be resolved after installation",
                    error_code="GRADLE_EXECUTABLE_NOT_RESOLVED",
                    suggestions=[
                        "Register a Gradle executable with the toolchain manager",
                        "Commit and use the project Gradle wrapper",
                        "Check whether the active environment overlay blocks the available Gradle executable",
                    ],
                    metadata={"working_directory": working_directory},
                )

        # Validate that build.gradle or build.gradle.kts exists
        build_validation = self._validate_build_file_exists(working_directory, build_file)
        if not build_validation["exists"]:
            return self._handle_missing_build_file(build_validation, working_directory)

        # Check if this is a multi-module project running tests without fail handling
        is_multi_module = self._is_multi_module_project(working_directory)
        test_tasks = ["test", "check", "Test", "integrationTest", "functionalTest"]
        is_test_task = tasks and any(task in tasks for task in test_tasks)

        if is_test_task and is_multi_module:
            modules_info = self._get_module_info(working_directory)
            if not fail_at_end:
                logger.warning(
                    "⚠️ Multi-module project detected! Gradle will STOP at first module with test failures."
                )
                logger.warning(
                    f"📦 Found {modules_info.get('module_count', 'multiple')} modules: {', '.join(modules_info.get('modules', [])[:5])}"
                )
                logger.info(
                    "💡 RECOMMENDED: gradle(tasks='test', fail_at_end=True) to test ALL modules"
                )
                logger.info(f"💡 Current approach will only test modules until first failure!")

        # Handle fail_at_end for test tasks
        if fail_at_end and is_test_task:
            logger.info("📝 Enabling test failure ignore for fail_at_end with test tasks")
            logger.info("   (Gradle's --continue will process all modules even with failures)")
            # Add test.ignoreFailures property
            if properties:
                if "test.ignoreFailures" not in properties:
                    properties += " -Dtest.ignoreFailures=true"
            else:
                properties = "-Dtest.ignoreFailures=true"

        # Build Gradle command
        gradle_cmd = self._build_gradle_command(
            gradle_executable,
            tasks,
            properties,
            gradle_args,
            build_file,
            parallel,
            configure_on_demand,
            build_cache,
            fail_at_end,
        )

        # Execute the command
        try:
            # Any real Gradle task can be long (jar, javadoc, custom tasks...);
            # only known-quick introspection commands stay on the blocking path
            # with a hard timeout. Everything else goes dispatch-and-poll so a
            # legitimately long build is never killed mid-run.
            quick_markers = ("help", "tasks", "projects", "properties", "--version")
            is_long_running = not any(marker in gradle_cmd for marker in quick_markers)

            def _run_build():
                if is_long_running and hasattr(
                    self.orchestrator, "execute_command_with_soft_timeout"
                ):
                    # Dispatch-and-poll: run detached with a soft window; if still
                    # running when it closes, hand the log tail back to the agent
                    # instead of killing a legitimately long build.
                    logger.info(f"Executing Gradle command via dispatch-and-poll: {gradle_cmd}")
                    try:
                        return self.orchestrator.execute_command_with_soft_timeout(
                            gradle_cmd,
                            workdir=working_directory,
                            hold=dispatch_hold_policy("gradle", gradle_cmd),
                        )
                    except TypeError as exc:
                        # Small test orchestrators predate the hold parameter.
                        if "hold" not in str(exc):
                            raise
                        return self.orchestrator.execute_command_with_soft_timeout(
                            gradle_cmd,
                            workdir=working_directory,
                        )
                if is_long_running and hasattr(
                    self.orchestrator, "execute_command_with_monitoring"
                ):
                    # Use monitoring version with extended timeouts for build commands
                    logger.info(f"Executing Gradle command with extended timeout: {gradle_cmd}")
                    return self.orchestrator.execute_command_with_monitoring(
                        gradle_cmd,
                        workdir=working_directory,
                        silent_timeout=1200,  # 20 minutes for no output (dependency downloads)
                        absolute_timeout=3600,  # 60 minutes total
                        optimize_for_maven=True,  # Use Maven optimization (works for both Maven and Gradle)
                    )
                # Use regular version for quick commands like help, tasks, etc.
                return self.orchestrator.execute_command(
                    gradle_cmd, workdir=working_directory, timeout=timeout
                )

            # One contract authorizes one executor/action/cwd/vector. A valid
            # contract for another executor is no authority at all here.
            contract, _created = ensure_dispatch_contract(
                self.orchestrator.execute_command,
                tool="gradle",
                effective_action=str(tasks or "build"),
                expected_cwd=working_directory,
                expected_argv=gradle_cmd,
                requirements=requirements,
            )
            if contract is None:
                return ToolResult.completed_failure(
                    output=(
                        "[contract] Gradle was not dispatched: the active contract does "
                        "not match this executor, action, cwd, and argv."
                    ),
                    error="invocation contract authority mismatch",
                    error_code=CONTRACT_AUTHORITY_MISSING,
                    metadata={"runner_dispatched": False, "tool": "gradle"},
                )

            def _run_build_with_receipt(attempt: int):
                # P0-A: bracket the physical dispatch with report-XML content
                # hashes so the reports THIS invocation wrote are attributable,
                # instead of being inferred from a later global scan.
                with dispatch_contract(contract):
                    before = snapshot_reports(
                        self.orchestrator.execute_command, [working_directory]
                    )
                    dispatched = _run_build()
                    self._record_invocation_receipt(
                        requested_action=tasks,
                        argv=gradle_cmd,
                        working_directory=working_directory,
                        attempt=attempt,
                        result=dispatched,
                        before=before,
                        requirements=requirements,
                        # What Gradle itself ran, module by module — the
                        # coverage denominator is built from this rather than
                        # from every source tree on disk.
                        module_outcomes=_gradle_module_outcomes(
                            dispatched.get("full_output") or dispatched.get("output") or ""
                        ),
                        # Reports Gradle vouched for without rewriting them
                        # (`--build-cache`): claimable, and kept distinct from
                        # what this dispatch physically wrote.
                        cached_report_roots=_gradle_cached_report_dirs(
                            dispatched.get("full_output") or dispatched.get("output") or "",
                            working_directory,
                        ),
                    )
                return dispatched

            result = _run_build_with_receipt(1)

            # Bounded retry: a version-shaped failure means the requirement in
            # the error text is authoritative; re-provision from it and rerun
            # ONCE (spec §1c: exactly one retry, never more). Owned by the
            # facade on the facade path (outcome is None there): a retry on
            # BOTH layers would mean two version-driven reruns per build.
            jdk_retry_meta = None
            if (
                outcome is not None
                and result.get("exit_code") != 0
                and not result.get("dispatch_status")
                and not result.get("termination_reason")
            ):
                needed = classify_version_error(result.get("output") or "")
                active = outcome.active_version or active_java_major(self.orchestrator)
                if needed and needed != active:
                    retry_outcome = JdkPreflight(self.orchestrator).run(
                        needed, source="build-error"
                    )
                    if retry_outcome.provisioned:
                        preamble += (
                            f"[pre-flight] build error requires Java {needed}, "
                            f"re-provisioned, retry 1/1\n"
                        )
                        jdk_retry_meta = {"from": active, "to": needed}
                        result = _run_build_with_receipt(2)

            if result.get("dispatch_status") in DETACHED_HANDOFF_STATUSES:
                return detached_handoff_tool_result("gradle", gradle_cmd, result)

            if result.get("termination_reason"):
                return self._apply_invocation_receipt(
                    self._timeout_result_from_command(result, gradle_cmd, tasks)
                )

            # The complete log. Detached builds hand back a complete
            # `full_output` (untruncated) next to the bounded inline `output`;
            # the analysis is parsed by regex and never reaches the model's
            # context, so it reads THIS text and no truncation applies to it
            # (same defect Maven carried: a summary line in the omitted middle
            # was a summary line thrown away).
            full_output = result.get("full_output") or result["output"]

            # Analyze the output
            analysis = self._analyze_gradle_output(
                full_output,
                result["exit_code"],
                compile_source_languages=_compile_source_languages,
            )
            compile_mismatch = analysis.get("compile_source_mismatch")
            if compile_mismatch is None and _compile_source_languages is None:
                # P0-C follow-up (live p5v-bigtop-r1): the recovery path re-ran
                # spark's compile with a bare static task list, so no probe
                # accompanied the call and an all-NO-SOURCE run scored green.
                # The guard cannot depend on WHO invoked it: when every executed
                # compile task found nothing, probe the source dirs ourselves.
                compiled = [
                    t for t in analysis.get("tasks_executed") or [] if self._is_compile_task(t)
                ]
                no_source = [
                    t for t in analysis.get("no_source_tasks") or [] if self._is_compile_task(t)
                ]
                if compiled and set(no_source) == set(compiled):
                    self_probed = self._probe_compile_source_dirs(working_directory)
                    compile_mismatch = self._compile_coverage_error(analysis, self_probed)
                    if compile_mismatch:
                        analysis["compile_source_mismatch"] = compile_mismatch

            # The durable detached job log remains complete; output storage
            # keeps a bounded searchable excerpt plus the full-log reference.
            ref_id = None
            stored_output, log_storage_metadata = bounded_detached_log_excerpt(full_output, result)
            self._pending_log_storage_metadata = log_storage_metadata
            if len(full_output) > 800 or result.get("dispatch_status") == "completed_detached":
                if not self.output_storage:
                    contexts_dir = Path("/workspace/.setup_agent/contexts")
                    self.output_storage = OutputStorageManager(contexts_dir, self.orchestrator)

                ref_id = self.output_storage.store_output(
                    task_id=f"gradle_{working_directory.replace('/', '_')}",
                    tool_name="gradle",
                    output=stored_output,
                    metadata={
                        "command": gradle_cmd,
                        "exit_code": result["exit_code"],
                        **log_storage_metadata,
                    },
                )
                logger.debug(f"Stored Gradle output with ref_id: {ref_id}")

            if result.get("dispatch_status") == "completed_detached":
                detached_result = classify_detached_completion(
                    result.get("exit_code"),
                    str(result.get("output") or ""),
                    ref_id,
                    runner="gradle",
                    full_output=stored_output,
                    poll_ref=detached_poll_ref(result),
                    output_ref_storage=self.output_storage,
                    invocation_status=(
                        "crashed" if result.get("lifecycle_state") == "vanished" else "completed"
                    ),
                    terminal_observation=True,
                )
                if not detached_result.succeeded:
                    detached_result.metadata.update(
                        {
                            "command": gradle_cmd,
                            "runner_dispatched": result.get("runner_dispatched") is True,
                            "exit_code": result.get("exit_code"),
                            "analysis": analysis,
                            "dispatch_status": "completed_detached",
                            "output_ref_id": ref_id,
                        }
                    )
                    return self._finalize_main_result(
                        # A dispatch that died still wrote what it wrote: the
                        # reports are on disk and claimed by this receipt, and
                        # gradle's own aggregate is in the log the classifier
                        # just sealed. The death stays the result's verdict.
                        harvest_detached_evidence(
                            detached_result,
                            self._gradle_evidence_fields(analysis, ref_id),
                        ),
                        preamble,
                        jdk_retry_meta,
                    )

            if raw_output:
                evidence_fields = self._gradle_evidence_fields(analysis, ref_id)
                return self._finalize_main_result(
                    ToolResult.completed(
                        operation_outcome=(
                            "success"
                            if result["exit_code"] == 0 and not compile_mismatch
                            else "failed"
                        ),
                        output=result["output"],
                        raw_output=result["output"],
                        error=compile_mismatch,
                        **evidence_fields,
                        metadata={
                            "command": gradle_cmd,
                            "runner_dispatched": result.get("runner_dispatched") is True,
                            "exit_code": result["exit_code"],
                            "analysis": analysis,
                            "output_ref_id": ref_id,
                        },
                    ),
                    preamble,
                    jdk_retry_meta,
                )

            evidence_fields = self._gradle_evidence_fields(analysis, ref_id)
            if result["exit_code"] == 0 and compile_mismatch:
                # Gradle's own exit code says SUCCESS; the task outcomes say the
                # compile never touched the sources. The narrower fact wins, and
                # the evidence must say so or the domain gate would score this
                # domain green from exit 0. Plan 6 Stage 0: that verdict is an
                # append-only assessment NEXT TO the receipt — the receipt is
                # finalized once and states only what gradle physically did.
                self._record_receipt_assessment(
                    typed_code="compile_no_source_mismatch",
                    detail=compile_mismatch,
                )
                return self._finalize_main_result(
                    ToolResult.completed_failure(
                        output=self._format_compile_coverage_failure(
                            analysis, compile_mismatch, ref_id
                        ),
                        raw_output=result["output"],
                        error=compile_mismatch,
                        error_code="GRADLE_COMPILE_NO_SOURCE_MISMATCH",
                        suggestions=[
                            "Confirm which compile tasks the build declares: "
                            "gradle(tasks='tasks --all')",
                            "A src/main/<lang> directory with no matching gradle plugin "
                            "(scala/kotlin/groovy) has no compile task — check the build file",
                        ],
                        **evidence_fields,
                        metadata={
                            "command": gradle_cmd,
                            "runner_dispatched": result.get("runner_dispatched") is True,
                            "exit_code": result["exit_code"],
                            "analysis": analysis,
                            "output_ref_id": ref_id,
                        },
                    ),
                    preamble,
                    jdk_retry_meta,
                )
            if result["exit_code"] == 0:
                return self._finalize_main_result(
                    ToolResult.completed_success(
                        output=self._format_success_output_enhanced(analysis, ref_id),
                        raw_output=result["output"],
                        **evidence_fields,
                        metadata={
                            "command": gradle_cmd,
                            "runner_dispatched": result.get("runner_dispatched") is True,
                            "exit_code": result["exit_code"],
                            "analysis": analysis,
                            "output_ref_id": ref_id,
                        },
                    ),
                    preamble,
                    jdk_retry_meta,
                )
            else:
                return self._finalize_main_result(
                    self._handle_gradle_error(
                        result["output"],
                        result["exit_code"],
                        gradle_cmd,
                        analysis,
                        runner_dispatched=result.get("runner_dispatched") is True,
                        output_ref_id=ref_id,
                    ),
                    preamble,
                    jdk_retry_meta,
                )

        except Exception as e:
            raise ToolError(
                message=f"Failed to execute Gradle command: {str(e)}",
                suggestions=[
                    "Check if Gradle wrapper (gradlew) exists in the project",
                    "Verify the working directory contains a build.gradle or build.gradle.kts file",
                    "Check Docker container connectivity",
                    "Try running with --stacktrace for more details",
                ],
                documentation_links=[
                    "https://docs.gradle.org/current/userguide/gradle_wrapper.html",
                    "https://docs.gradle.org/current/userguide/command_line_interface.html",
                ],
                error_code="GRADLE_EXECUTION_ERROR",
            )

    def _finalize_main_result(
        self,
        tool_result: ToolResult,
        preamble: str,
        jdk_retry: Optional[Dict[str, str]] = None,
    ) -> ToolResult:
        """Prepend pre-flight/scope narration and record retry metadata.

        The narration is the feature (transparency-by-construction, spec
        §§1b-1c, 3): whatever the pre-flight did — or could not do — must be
        visible in the agent's observation, not just in host logs.
        """
        if preamble:
            tool_result.output = preamble + (tool_result.output or "")
            tool_result.raw_output = preamble + (tool_result.raw_output or "")
        if jdk_retry:
            tool_result.metadata["jdk_retry"] = jdk_retry
        tool_result.metadata.update(getattr(self, "_pending_log_storage_metadata", {}) or {})
        return self._apply_invocation_receipt(tool_result)

    def _record_invocation_receipt(
        self,
        *,
        requested_action: str,
        argv: str,
        working_directory: str,
        attempt: int,
        result: Dict[str, Any],
        before: Dict[str, str],
        requirements: Optional[Dict[str, Any]] = None,
        module_outcomes: Optional[List[Dict[str, str]]] = None,
        cached_report_roots: Optional[List[str]] = None,
    ) -> None:
        """Persist the P0-A invocation receipt for one physical dispatch.

        Gradle runs the task list it was handed (the facade already mapped the
        verb), so the two actions coincide — EXCEPT when no task was named at
        all, where _build_gradle_command substitutes `build`. That
        substitution is exactly the kind of divergence the receipt exists to
        record, so it is written down rather than smoothed over.

        A build still in flight (detached hand-off) has no terminal outcome
        yet, so it mints no receipt here — but the evidence it is holding is
        no longer dropped: the `before` snapshot, the frozen contract and the
        detach handle become one obligation (Plan 8 §3.1), and settlement
        writes the ordinary receipt when the job's exit code lands.
        """
        self._pending_invocation_receipt = None
        requested = " ".join(shlex.split(str(requested_action or "")))
        if result.get("dispatch_status") in DETACHED_HANDOFF_STATUSES:
            obligation = record_dispatch_obligation_result(
                self.orchestrator.execute_command,
                result=result,
                tool="gradle",
                attempt=attempt,
                requested_action=requested,
                effective_action=requested or "build",
                argv=argv,
                working_directory=working_directory,
                before=before,
                requirements=requirements,
            )
            result.update(obligation.metadata())
            return
        after = snapshot_reports(self.orchestrator.execute_command, [working_directory])
        # What this dispatch left on disk, read under the caps the receipt
        # records. It runs HERE, after the bracket and before the receipt is
        # assembled, because the report tree is only this invocation's evidence
        # for as long as no other dispatch has touched it.
        harvest = gradle_test_harvest(
            self.orchestrator.execute_command,
            working_directory=working_directory,
            delta=report_delta(before, after, cached_report_roots),
            test_dispatch=gradle_test_action(requested, requested or "build"),
        )
        self._pending_invocation_receipt = record_invocation(
            self.orchestrator.execute_command,
            tool="gradle",
            attempt=attempt,
            requested_action=requested,
            effective_action=requested or "build",
            argv=argv,
            working_directory=working_directory,
            exit_code=result.get("exit_code"),
            before=before,
            after=after,
            # HOW this dispatch ended. A detached job whose process vanished
            # carries a synthesized exit code and a log truncated at the kill;
            # the exit code alone cannot say so, and a reader of the receipt
            # must be able to tell.
            lifecycle_state=result.get("lifecycle_state"),
            termination_reason=result.get("termination_reason"),
            module_outcomes=_gradle_module_outcomes_with_counts(
                module_outcomes or (), harvest.module_tests_reported
            ),
            gradle_suite_summaries=harvest.suite_summaries,
            gradle_row_disclosure=harvest.row_disclosure,
            harvested_testcase_outcomes=harvest.testcase_outcomes,
            declared_omissions=harvest.omissions,
            cached_report_roots=cached_report_roots,
            output=result.get("full_output") or result.get("output"),
            requirements=requirements,
            # Plan 6 Stage B: bind this dispatch back to the contract the build
            # facade froze for it. Absent when the runner was called outside
            # the facade, and `compliance` is the argv comparison's verdict.
            **contract_receipt_fields(argv),
        )

    def _record_receipt_assessment(self, *, typed_code: str, detail: str) -> bool:
        """Append this classifier's verdict next to THIS invocation's receipt.

        Spec §C4: the receipt is finalized once. A run whose receipt never
        landed has nothing to assess — the missing receipt is already reported
        as `receipt_persisted: false`.
        """
        pending = getattr(self, "_pending_invocation_receipt", None) or {}
        receipt_id = str(pending.get("receipt_id") or "").strip()
        if not receipt_id:
            return False
        return write_assessment(
            self.orchestrator.execute_command,
            ReceiptAssessment(receipt_id=receipt_id, typed_code=typed_code, detail=detail),
        )

    def _apply_invocation_receipt(self, tool_result: ToolResult) -> ToolResult:
        """Byte-compat: `receipt_id` on success, `receipt_persisted` only on
        failure — nothing at all on paths that dispatched no runner."""
        receipt_metadata = getattr(self, "_pending_invocation_receipt", None)
        if receipt_metadata:
            tool_result.metadata.update(receipt_metadata)
        return tool_result

    def _resolve_gradle_executable(self, working_directory: str, prefer_wrapper: bool = True):
        if not self.toolchain_manager:
            return None
        return self.toolchain_manager.resolve(
            ToolchainSpec(
                name="gradle",
                executable="gradle",
                prefer_wrapper=prefer_wrapper,
            ),
            working_directory=working_directory,
        )

    def _determine_gradle_executable(
        self, working_directory: str, use_wrapper: bool, resolved_gradle=None
    ) -> Optional[str]:
        """Determine which Gradle executable to use."""
        if resolved_gradle and resolved_gradle.candidate.source == "env_overlay":
            return resolved_gradle.candidate.path

        if use_wrapper and resolved_gradle and resolved_gradle.candidate.source == "wrapper":
            wrapper = resolved_gradle.candidate.path
            chmod = self.orchestrator.execute_command(
                f"chmod +x {shlex.quote(wrapper)}",
                workdir=working_directory,
            )
            if chmod.get("exit_code") == 0:
                logger.info(f"Found checkout Gradle wrapper: {wrapper}")
                return wrapper
            logger.warning(f"Checkout Gradle wrapper is not executable: {wrapper}")
            resolved_gradle = None

        if use_wrapper:
            # Check for gradlew wrapper
            wrapper_check = self.orchestrator.execute_command(
                f"test -f {working_directory}/gradlew && echo 'exists'", workdir=working_directory
            )
            if wrapper_check.get("exit_code") == 0 and "exists" in wrapper_check.get("output", ""):
                logger.info("Found Gradle wrapper (gradlew)")
                # Make sure it's executable
                self.orchestrator.execute_command(
                    f"chmod +x {working_directory}/gradlew", workdir=working_directory
                )
                return "./gradlew"

        if resolved_gradle:
            return resolved_gradle.candidate.path

        if self.toolchain_manager:
            return None

        # Check for system Gradle
        gradle_check = self.orchestrator.execute_command("which gradle")
        if gradle_check.get("exit_code") == 0:
            logger.info("Found system Gradle")
            return "gradle"

        return None

    def _install_gradle(self, working_directory: str) -> ToolResult:
        """Install Gradle."""
        logger.info("Installing Gradle...")

        # Install system Gradle
        install_cmd = "apt-get update && " "apt-get install -y gradle"

        result = self.orchestrator.execute_command(install_cmd, timeout=300)

        if result.get("exit_code") == 0:
            return ToolResult.completed_success(output="✅ Gradle installed successfully")
        else:
            raise ToolError(
                message="Failed to install Gradle",
                suggestions=[
                    "Check if the container has internet access",
                    "Try installing Gradle manually",
                    "Ensure the Gradle wrapper is committed to the repository",
                ],
                error_code="GRADLE_INSTALLATION_FAILED",
            )

    def _validate_build_file_exists(
        self, working_directory: str, build_file: str = None
    ) -> Dict[str, Any]:
        """Validate that a Gradle build file exists."""
        if build_file:
            # Check specific build file
            check_cmd = f"test -f {working_directory}/{build_file}"
        else:
            # Check for standard build files
            check_cmd = (
                f"test -f {working_directory}/build.gradle || "
                f"test -f {working_directory}/build.gradle.kts || "
                f"test -f {working_directory}/settings.gradle || "
                f"test -f {working_directory}/settings.gradle.kts"
            )

        result = self.orchestrator.execute_command(check_cmd, workdir=working_directory)

        if result.get("exit_code") == 0:
            return {"exists": True}

        # Try to find build files in subdirectories
        find_result = self.orchestrator.execute_command(
            "find . -maxdepth 3 -name 'build.gradle' -o -name 'build.gradle.kts' -o -name 'settings.gradle' -o -name 'settings.gradle.kts' | head -10",
            workdir=working_directory,
        )

        found_files = []
        if find_result.get("exit_code") == 0 and find_result.get("output"):
            found_files = [
                f.strip() for f in find_result["output"].strip().split("\n") if f.strip()
            ]

        return {"exists": False, "searched_in": working_directory, "found_files": found_files}

    def _handle_missing_build_file(self, validation: Dict, working_directory: str) -> ToolResult:
        """Handle missing build.gradle file."""
        suggestions = [
            f"Change to the correct directory containing build.gradle",
            "Ensure the project has been properly cloned or initialized",
        ]

        if validation.get("found_files"):
            suggestions.insert(
                0, f"Found build files in: {', '.join(validation['found_files'][:3])}"
            )
            suggestions.insert(1, f"Try changing working_directory to the correct path")

        raise ToolError(
            message=f"No build.gradle or build.gradle.kts found in {working_directory}",
            suggestions=suggestions,
            documentation_links=[
                "https://docs.gradle.org/current/userguide/tutorial_using_tasks.html"
            ],
            error_code="BUILD_FILE_NOT_FOUND",
        )

    def _build_gradle_command(
        self,
        executable: str,
        tasks: str,
        properties: str,
        gradle_args: str,
        build_file: str,
        parallel: bool,
        configure_on_demand: bool,
        build_cache: bool,
        fail_at_end: bool = False,
    ) -> str:
        """Build the complete Gradle command."""
        cmd_parts = [executable]

        # Add build file if specified
        if build_file:
            cmd_parts.extend(["-b", build_file])

        # Add fail_at_end support (--continue flag)
        if fail_at_end:
            cmd_parts.append("--continue")

        # Add performance flags
        if parallel:
            cmd_parts.append("--parallel")
        if configure_on_demand:
            cmd_parts.append("--configure-on-demand")
        if build_cache:
            cmd_parts.append("--build-cache")

        # Add properties
        if properties:
            # Handle both space and comma-separated properties
            props = properties.replace(",", " ").split()
            for prop in props:
                if not prop.startswith("-P") and not prop.startswith("-D"):
                    prop = f"-P{prop}"
                cmd_parts.append(prop)

        # Add gradle arguments verbatim — the caller's quoting is the caller's
        # (`--tests "*ProducerTest*"`), and re-splitting it here would rewrite
        # a vector the contract already froze.
        if gradle_args:
            cmd_parts.append(gradle_args)

        # The contracted task set (dispatch_argv is the single producer, shared
        # with GradleBackend.expected_argv): a scoped selection the caller
        # already made is never joined by its bare superset, and `build` stands
        # in only when neither side names a task.
        cmd_parts.extend(gradle_task_tokens(tasks, gradle_args))

        return " ".join(cmd_parts)

    def _timeout_result_from_command(
        self, result: Dict[str, Any], gradle_cmd: str, tasks: str
    ) -> ToolResult:
        reason = str(result.get("termination_reason") or "unknown")
        sanitized_reason = re.sub(r"[^A-Za-z0-9]+", "_", reason).strip("_").upper()
        error_code = f"TIMEOUT_{sanitized_reason or 'UNKNOWN'}"
        execution_time = result.get("execution_time", 0)
        try:
            execution_time_display = float(execution_time)
        except (TypeError, ValueError):
            execution_time_display = 0.0
        task_name = tasks or "build"
        suggestions = [
            "Break the Gradle build into smaller tasks",
            "Run dependency resolution before the full build",
            "Retry with --info or --debug to inspect progress",
        ]
        return ToolResult.terminal_failure(
            invocation_status="timeout",
            output=(
                f"Gradle task timed out due to {reason} after " f"{execution_time_display:.1f}s."
            ),
            error=f"Gradle task timed out ({reason})",
            error_code=error_code,
            suggestions=suggestions,
            raw_output=result.get("output"),
            metadata={
                "termination_reason": reason,
                "execution_time": execution_time,
                "command": gradle_cmd,
                "runner_dispatched": result.get("runner_dispatched") is True,
                "task": task_name,
                "exit_code": result.get("exit_code"),
                "tool_type": "gradle",
            },
        )

    @staticmethod
    def _is_compile_task(task: str) -> bool:
        """A compile task, whatever project path qualifies it (:sub:compileScala)."""
        return task.rsplit(":", 1)[-1].startswith("compile")

    def _probe_compile_source_dirs(self, working_directory: str) -> List[str]:
        """Compile languages whose src/main/<lang> exists under the root —
        the same directory facts the backend probes, self-served so the
        NO-SOURCE guard holds on EVERY invocation path (recovery included)."""
        root = working_directory.rstrip("/")
        probe = self.orchestrator.execute_command(
            f"for lang in scala kotlin groovy; do "
            f'test -d {shlex.quote(root)}/src/main/"$lang" && echo "$lang"; done; true'
        )
        if not probe.get("success"):
            return []
        return [line.strip() for line in (probe.get("output") or "").splitlines() if line.strip()]

    def _compile_coverage_error(
        self, analysis: Dict[str, Any], compile_source_languages: Optional[List[str]]
    ) -> Optional[str]:
        """NO-SOURCE cannot close a source-bearing compile (P0-C).

        Live bigtop: bigpetstore-spark's sources are all under src/main/scala, the
        backend ran compileJava, gradle answered NO-SOURCE, the exit code was 0 —
        and the harness recorded a green compile of a module it never compiled.
        A compile whose EVERY executed compile task found nothing to do has not
        covered sources the caller confirmed on disk.
        """
        languages = sorted({str(lang) for lang in (compile_source_languages or []) if lang})
        if not languages:
            return None
        compiled = [t for t in analysis.get("tasks_executed") or [] if self._is_compile_task(t)]
        if not compiled:
            return None
        no_source = [t for t in analysis.get("no_source_tasks") or [] if self._is_compile_task(t)]
        if set(no_source) != set(compiled):
            return None
        return (
            f"{', '.join(languages)} sources present; executed compile tasks all "
            "reported NO-SOURCE — the compile did not cover the sources"
        )

    def _analyze_gradle_output(
        self,
        output: str,
        exit_code: int,
        compile_source_languages: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Analyze Gradle output for important information."""
        analysis = {
            "exit_code": exit_code,
            "build_successful": False,
            "test_results": None,
            "compilation_errors": [],
            "test_failures": [],
            "dependency_errors": [],
            "warnings": [],
            "deprecated_features": [],
            "build_time": None,
            "tasks_executed": [],
            "no_source_tasks": [],
            "cache_hits": 0,
        }

        lines = output.split("\n")

        for i, line in enumerate(lines):
            # Check for build success
            if "BUILD SUCCESSFUL" in line:
                analysis["build_successful"] = True
            elif "BUILD FAILED" in line:
                analysis["build_successful"] = False

            # Extract test results
            if "tests completed" in line.lower() or "test run:" in line.lower():
                if not analysis["test_results"]:
                    analysis["test_results"] = {}

                test_match = re.search(r"(\d+)\s+tests?\s+completed", line, re.IGNORECASE)
                if not test_match:
                    test_match = re.search(r"test run:\s*(\d+)\s+tests?", line, re.IGNORECASE)
                if test_match:
                    analysis["test_results"]["total"] = int(test_match.group(1))

                # Look for failures and errors
                fail_match = re.search(r"(\d+)\s+failed", line, re.IGNORECASE)
                if fail_match:
                    analysis["test_results"]["failed"] = int(fail_match.group(1))

                skipped_match = re.search(r"(\d+)\s+skipped", line, re.IGNORECASE)
                if skipped_match:
                    analysis["test_results"]["skipped"] = int(skipped_match.group(1))

            # Check for compilation errors
            if "compilation failed" in line.lower() or "compiler error" in line.lower():
                # Extract error details from surrounding lines
                error_context = lines[max(0, i - 2) : min(len(lines), i + 3)]
                analysis["compilation_errors"].append("\n".join(error_context))

            # Check for dependency resolution errors
            if (
                "could not resolve" in line.lower()
                or "dependency" in line.lower()
                and "not found" in line.lower()
            ):
                analysis["dependency_errors"].append(line.strip())

            # Extract build time
            if "Total time:" in line:
                time_match = re.search(r"Total time:\s+(.+)", line)
                if time_match:
                    analysis["build_time"] = time_match.group(1)

            # Track executed tasks, and the outcome gradle printed after each —
            # NO-SOURCE means the task found nothing to do, which is a fact
            # about coverage, not about success.
            if "> Task :" in line:
                task_match = re.search(r"> Task :(\S+)(.*)$", line)
                if task_match:
                    analysis["tasks_executed"].append(task_match.group(1))
                    if "NO-SOURCE" in task_match.group(2):
                        analysis["no_source_tasks"].append(task_match.group(1))

            # Check for cache hits (Gradle-specific)
            if "FROM-CACHE" in line or "UP-TO-DATE" in line:
                analysis["cache_hits"] += 1

            # Deprecated features warning
            if "deprecated" in line.lower():
                analysis["deprecated_features"].append(line.strip())

        mismatch = self._compile_coverage_error(analysis, compile_source_languages)
        if mismatch:
            analysis["build_successful"] = False
            analysis["compile_source_mismatch"] = mismatch

        return analysis

    def _format_success_output(self, analysis: Dict[str, Any]) -> str:
        """Format successful Gradle execution output."""
        output = "✅ Gradle build completed successfully!\n\n"

        if analysis.get("tasks_executed"):
            output += f"📋 Tasks executed: {', '.join(analysis['tasks_executed'][:5])}\n"
            if len(analysis["tasks_executed"]) > 5:
                output += f"   ... and {len(analysis['tasks_executed']) - 5} more\n"

        if analysis.get("test_results"):
            results = analysis["test_results"]
            output += f"🧪 Test Results:\n"
            output += f"   Total: {results.get('total', 0)}\n"
            if results.get("failed", 0) > 0:
                output += f"   ❌ Failed: {results['failed']}\n"
            else:
                output += f"   ✅ All tests passed\n"

        if analysis.get("build_time"):
            output += f"⏱️ Build time: {analysis['build_time']}\n"

        if analysis.get("cache_hits", 0) > 0:
            output += f"🚀 Cache optimization: {analysis['cache_hits']} tasks cached\n"

        if analysis.get("deprecated_features"):
            output += f"⚠️ Deprecated features detected: {len(analysis['deprecated_features'])} warnings\n"

        return output

    def _format_success_output_enhanced(
        self, analysis: Dict[str, Any], ref_id: Optional[str] = None
    ) -> str:
        """Format with essential validation data always visible."""
        output = "✅ Gradle build completed\n\n"

        # ALWAYS show what tasks executed (critical for validation)
        output += "📍 Tasks executed: "
        if analysis.get("tasks_executed"):
            tasks = analysis["tasks_executed"]
            output += ", ".join(tasks[:5])
            if len(tasks) > 5:
                output += f" (+{len(tasks)-5} more)"
        else:
            output += "⚠️ NONE DETECTED (possible parsing issue)"

        # ALWAYS show test execution status if test task ran
        test_tasks = ["test", "check", "Test", "Check"]
        tasks_executed = analysis.get("tasks_executed", [])
        if any(any(test_task in task for test_task in test_tasks) for task in tasks_executed):
            output += "\n📊 Test Execution: "
            if analysis.get("test_results"):
                results = analysis["test_results"]
                total = results.get("total", 0)
                failed = results.get("failed", 0)
                output += f"{total} tests run"
                if failed > 0:
                    output += f", {failed} failed ❌"
                else:
                    output += " ✅"
            else:
                output += "⚠️ Test task ran but no results captured (check build/reports/tests/)"

        # Show compilation status if compile tasks ran
        compile_tasks = ["compileJava", "compileKotlin", "compile"]
        if any(
            any(compile_task in task for compile_task in compile_tasks) for task in tasks_executed
        ):
            if analysis.get("compilation_errors"):
                output += f"\n❌ Compilation: {len(analysis['compilation_errors'])} errors"
            else:
                output += "\n✅ Compilation: successful"

        # Show build status
        if analysis.get("build_successful"):
            output += "\n✅ Build: SUCCESS"
        else:
            output += "\n❌ Build: FAILED"

        # Reference to full output
        if ref_id:
            output += f"\n\n📄 Full output reference: {ref_id}"
            output += (
                f"\n💡 Use: search(target='{ref_id}') for the complete log, or "
                f"search(target='{ref_id}', pattern='ERROR') to grep it"
            )

        # Cache and performance info
        if analysis.get("cache_hits", 0) > 0:
            output += f"\n🚀 Performance: {analysis['cache_hits']} tasks cached"

        # Warnings summary
        if analysis.get("deprecated_features"):
            output += f"\n⚠️ {len(analysis['deprecated_features'])} deprecation warnings (see full output)"

        return output

    def _format_compile_coverage_failure(
        self, analysis: Dict[str, Any], message: str, ref_id: Optional[str] = None
    ) -> str:
        """Report the uncovered compile with the task outcomes that prove it."""
        output = "❌ Gradle reported success over a compile that covered no sources\n\n"
        tasks = analysis.get("tasks_executed") or []
        output += "📍 Tasks executed: "
        output += ", ".join(tasks) if tasks else "⚠️ NONE DETECTED (possible parsing issue)"
        no_source = analysis.get("no_source_tasks") or []
        if no_source:
            output += f"\n🚫 NO-SOURCE: {', '.join(no_source)}"
        output += f"\n⚠️ {message}"
        if analysis.get("build_time"):
            output += f"\n⏱️ Build time: {analysis['build_time']}"
        if ref_id:
            output += f"\n\n📄 Full output reference: {ref_id}"
            output += (
                f"\n💡 Use: search(target='{ref_id}') for the complete log, or "
                f"search(target='{ref_id}', pattern='ERROR') to grep it"
            )
        return output

    def _gradle_test_stats(self, analysis: Dict[str, Any]) -> Optional[TestStats]:
        results = analysis.get("test_results") or {}
        executed = int(results.get("total") or 0)
        if executed <= 0:
            return None

        failed = int(results.get("failed") or 0)
        skipped = int(results.get("skipped") or 0)
        passed = max(executed - failed - skipped, 0)
        return TestStats(
            executed=executed,
            passed=passed,
            failed=failed,
            skipped=skipped,
        )

    def _gradle_evidence_fields(
        self, analysis: Dict[str, Any], output_ref_id: Optional[str] = None
    ) -> Dict[str, Any]:
        fields: Dict[str, Any] = {}
        test_stats = self._gradle_test_stats(analysis)
        if test_stats:
            fields["test_stats"] = test_stats

        has_test_failures = bool(test_stats and test_stats.failed > 0)
        build_claimed_success = bool(
            analysis.get("build_successful") or analysis.get("exit_code") == 0
        )
        if has_test_failures and build_claimed_success:
            fields["evidence_assessment"] = EvidenceAssessment.PARTIAL
            fields["conflicts"] = ["gradle_success_vs_test_failures"]

        # Gradle said SUCCESSFUL while every compile task it ran said NO-SOURCE:
        # two facts from the same log that cannot both be trusted.
        if analysis.get("compile_source_mismatch"):
            fields.setdefault("conflicts", []).append("gradle_success_vs_compile_no_source")

        if output_ref_id:
            fields["evidence_refs"] = [output_ref_id]
            fields["output_ref_storage"] = self.output_storage

        return fields

    def _handle_gradle_error(
        self,
        output: str,
        exit_code: int,
        command: str,
        analysis: Dict[str, Any],
        runner_dispatched: bool = False,
        output_ref_id: Optional[str] = None,
    ) -> ToolResult:
        """Handle Gradle execution errors with detailed analysis."""
        error_message = f"Gradle command failed with exit code {exit_code}"
        suggestions = []

        # Analyze specific error patterns
        if analysis.get("compilation_errors"):
            error_message = "Compilation failed"
            suggestions.extend(
                [
                    "Check the Java source code for syntax errors",
                    "Verify all imports are correct",
                    "Ensure all dependencies are properly declared",
                    "Run with --stacktrace for detailed error information",
                ]
            )

        elif analysis.get("test_failures"):
            error_message = f"Tests failed: {len(analysis['test_failures'])} test(s) failed"
            suggestions.extend(
                [
                    "Review the test failure details in the output",
                    "Run specific failing tests with --tests <TestClass>",
                    "Check test logs in build/reports/tests/",
                    "Run with --info for more detailed test output",
                ]
            )

        elif analysis.get("dependency_errors"):
            error_message = "Dependency resolution failed"
            suggestions.extend(
                [
                    "Check your internet connection",
                    "Verify repository URLs in build.gradle",
                    "Try running with --refresh-dependencies",
                    "Check if required repositories are configured",
                    "Run 'gradle dependencies' to analyze dependency tree",
                ]
            )

        elif "permission denied" in output.lower():
            error_message = "Permission denied error"
            suggestions.extend(
                [
                    "Ensure gradlew is executable: chmod +x gradlew",
                    "Check file permissions in the project directory",
                    "Verify Docker container has proper permissions",
                ]
            )

        elif "out of memory" in output.lower() or "heap space" in output.lower():
            error_message = "Out of memory error"
            suggestions.extend(
                [
                    "Increase JVM heap size with -Xmx flag",
                    "Add 'org.gradle.jvmargs=-Xmx2g' to gradle.properties",
                    "Close other applications to free memory",
                    "Use --no-daemon to avoid daemon memory issues",
                ]
            )

        elif "could not find or load main class" in output.lower():
            error_message = "Gradle wrapper or Java configuration issue"
            suggestions.extend(
                [
                    "Regenerate Gradle wrapper: gradle wrapper",
                    "Check JAVA_HOME environment variable",
                    "Verify Java installation with 'java -version'",
                    "Ensure gradle-wrapper.jar exists in gradle/wrapper/",
                ]
            )

        else:
            # Generic error handling
            suggestions.extend(
                [
                    "Run with --stacktrace option for more details",
                    "Run with --info or --debug for verbose output",
                    "Check build.gradle for configuration errors",
                    "Try running 'gradle clean' before building",
                    "Verify all required plugins are properly configured",
                ]
            )

        # Extract the most relevant error snippet
        error_snippet = self._extract_gradle_key_info(output)

        metadata = {
            "exit_code": exit_code,
            "command": command,
            "runner_dispatched": runner_dispatched,
            "analysis": analysis,
            "error_snippet": error_snippet,
        }
        if output_ref_id:
            metadata["output_ref_id"] = output_ref_id

        evidence_fields = self._gradle_evidence_fields(analysis, output_ref_id)
        return ToolResult.completed_failure(
            output=error_snippet,
            error=error_message,
            error_code="GRADLE_BUILD_FAILED",
            suggestions=suggestions,
            documentation_links=[
                "https://docs.gradle.org/current/userguide/troubleshooting.html",
                "https://docs.gradle.org/current/userguide/command_line_interface.html#sec:command_line_debugging",
            ],
            raw_output=output,
            metadata=metadata,
            **evidence_fields,
        )

    def _extract_gradle_key_info(self, output: str) -> str:
        """Extract key information from Gradle output."""
        if not output:
            return "No output"

        lines = output.split("\n")
        key_patterns = [
            r"BUILD FAILED",
            r"BUILD SUCCESSFUL",
            r"FAILURE:",
            r"> Task .* FAILED",
            r"error:",
            r"Error:",
            r"caused by:",
            r"\* What went wrong:",
            r"\* Try:",
            r"tests? failed",
            r"compilation failed",
            r"could not resolve",
        ]

        key_lines = []
        for i, line in enumerate(lines):
            for pattern in key_patterns:
                if re.search(pattern, line, re.IGNORECASE):
                    # Include some context
                    start = max(0, i - 2)
                    end = min(len(lines), i + 5)
                    key_lines.extend(lines[start:end])
                    break

        if key_lines:
            # Remove duplicates while preserving order
            seen = set()
            unique_lines = []
            for line in key_lines:
                if line not in seen:
                    seen.add(line)
                    unique_lines.append(line)
            return "\n".join(unique_lines[:50])  # Limit to 50 lines

        # If no key patterns found, return the last 30 lines
        return "\n".join(lines[-30:])

    def _is_multi_module_project(self, working_directory: str) -> bool:
        """Check if this is a multi-module Gradle project."""
        # Check for settings.gradle or settings.gradle.kts with include statements
        check_cmd = (
            "(test -f settings.gradle && grep -q 'include' settings.gradle) || "
            "(test -f settings.gradle.kts && grep -q 'include' settings.gradle.kts)"
        )
        result = self.orchestrator.execute_command(check_cmd, workdir=working_directory)
        return result.get("exit_code") == 0

    def _get_module_info(self, working_directory: str) -> Dict[str, Any]:
        """Get information about modules in a multi-module project."""
        modules = []

        # Try to extract module names from settings.gradle
        extract_cmd = (
            "if [ -f settings.gradle ]; then "
            "grep \"include\" settings.gradle | sed \"s/.*'\\([^']*\\)'.*/\\1/g\" | tr '\\n' ' '; "
            "elif [ -f settings.gradle.kts ]; then "
            "grep \"include\" settings.gradle.kts | sed 's/.*\"\\([^\"]*\\)\".*/\\1/g' | tr '\\n' ' '; "
            "fi"
        )

        result = self.orchestrator.execute_command(extract_cmd, workdir=working_directory)
        if result.get("exit_code") == 0 and result.get("output"):
            module_str = result["output"].strip()
            if module_str:
                # Split by space and clean up module names
                modules = [m.strip().replace(":", "") for m in module_str.split() if m.strip()]

        # If we couldn't extract from settings file, look for subdirectories with build.gradle
        if not modules:
            find_cmd = "find . -maxdepth 2 -name 'build.gradle' -o -name 'build.gradle.kts' | grep -v '^\\./build' | wc -l"
            count_result = self.orchestrator.execute_command(find_cmd, workdir=working_directory)
            if count_result.get("exit_code") == 0:
                try:
                    module_count = int(count_result["output"].strip())
                    if module_count > 1:
                        return {
                            "module_count": module_count,
                            "modules": ["(multiple modules detected)"],
                        }
                except ValueError:
                    pass

        return {
            "module_count": len(modules) if modules else 0,
            "modules": modules[:10] if modules else [],  # Limit to first 10 modules
        }

    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get parameters schema for function calling."""
        return {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "string",
                    "description": "Gradle tasks to execute (e.g., 'clean build', 'test', 'assemble')",
                },
                "properties": {
                    "type": "string",
                    "description": "Gradle properties (e.g., '-Pversion=1.0', '-PskipTests')",
                },
                "gradle_args": {
                    "type": "string",
                    "description": "Additional Gradle arguments (e.g., '--info', '--stacktrace', '--scan')",
                },
                "build_file": {
                    "type": "string",
                    "description": "Specific build file to use (e.g., 'custom.gradle')",
                },
                "raw_output": {
                    "type": "boolean",
                    "description": "Return raw Gradle output for detailed analysis",
                    "default": False,
                },
                "working_directory": {
                    "type": "string",
                    "description": "Directory to execute Gradle in",
                    "default": "/workspace",
                },
                "use_wrapper": {
                    "type": "boolean",
                    "description": "Prefer gradlew wrapper over system gradle",
                    "default": True,
                },
                "parallel": {
                    "type": "boolean",
                    "description": "Enable parallel execution",
                    "default": False,
                },
                "build_cache": {
                    "type": "boolean",
                    "description": "Use Gradle build cache",
                    "default": True,
                },
                "fail_at_end": {
                    "type": "boolean",
                    "description": "IMPORTANT for multi-module projects: Continue after task failures. For test tasks, adds test.ignoreFailures=true to run ALL tests even if some fail.",
                    "default": False,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds for quick commands. Long builds run detached with a soft window and are never hard-killed.",
                    "default": 300,
                },
            },
            "required": [],
        }

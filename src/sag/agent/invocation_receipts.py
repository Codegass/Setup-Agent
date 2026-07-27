"""Invocation receipts for runner calls (Plan 5 Stage B P0-A; Plan 6 Stage 0).

Ground-truth review 2026-07-26 (§"Evidence is snapshot-global instead of
receipt-scoped"): the validator scanned the project tree after several
invocations and treated every matching XML as current evidence. It could not
say which invocation wrote which report, so auxiliary reports and stale
retries entered the primary rollup (Bigtop's 54/54).

A receipt makes that answerable. Every physical maven/gradle/pytest runner
call brackets itself with a content-hash snapshot of the report XMLs under
its own scan roots and persists ONE atomic JSON file:

    /workspace/.setup_agent/invocation_receipts/<receipt_id>.json

Schema v2 (Plan 6 Stage 0, spec §C4) adds the binding facts the contract loop
needs before it can bind anything: the target sha, the survey/config pins the
run was decided on, the domain the invocation belongs to, the cwd it actually
used, its compliance class, the runner's own toolchain fingerprint, a content
hash of the output, and a bounded per-testcase outcome list parsed from THIS
invocation's report delta (review binding note (b)). Every schema-v1 key keeps
its exact name and shape — the Plan 5 consumers read v2 receipts unchanged —
and every v2 fact is absent when unknown, never null and never defaulted.

The receipt is finalized ONCE and never rewritten (spec §C4). Semantic
classification — "this exit 0 compiled nothing" — is an append-only
`ReceiptAssessment` in `evidence_assessments`, not an edit of this file.

Persistence is best effort HERE: this module never raises and never blocks
the command result the model is waiting for. A failed write is reported as a
fact (`receipt_persisted: false` in ToolResult metadata); turning that fact
into a closure failure is the phase gate's business, not the runner's.
"""

import hashlib
import html
import itertools
import json
import posixpath
import re
import shlex
import threading
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

RECEIPT_SCHEMA_VERSION = 2
RECEIPT_DIR = "/workspace/.setup_agent/invocation_receipts"
# Heredoc delimiter for the atomic write. The body is single-line JSON, so no
# receipt content can ever collide with it.
RECEIPT_HEREDOC = "SAGRECEIPT"

# Review binding note (b): the per-testcase list is bounded, and a truncation
# is recorded rather than silently dropped.
TESTCASE_OUTCOME_CAP = 50
# Transport bounds for the outcome parse. A surefire XML can carry megabytes of
# system-out, so the container returns only the report's TAGS (one round trip,
# `grep -oE`), never the report bodies.
TESTCASE_FILE_CAP = 50
TESTCASE_TAG_CAP = 400
TESTCASE_PARSE_CAP = 500
SKIP_REASON_MAX_CHARS = 200
# Marker that keeps an absent executable path from sliding into the version
# slot of a one-round-trip toolchain probe.
TOOLCHAIN_MARKER = "SAGTOOLCHAIN"
VERSION_LINE_MAX_CHARS = 200
# How each runner states its own version. `python -V`, `mvn -v`, `gradle -v`.
VERSION_FLAGS = {"maven": "-v", "gradle": "-v", "python": "-V"}
RUNNER_DEFAULTS = {"maven": "mvn", "gradle": "gradle", "python": "python"}

# A sha the container actually printed, not whatever text a broken probe
# echoed back (a fake/failing container answers every command with log text).
_OBJECT_NAME_RE = re.compile(r"[0-9a-f]{7,64}")
# JUnit report tags the outcome parse understands, in the container's own
# `grep -oE` token order.
TESTCASE_TAG_PATTERN = "<(testcase|skipped|failure|error)[^>]*>|</testcase>"
_TESTCASE_TAG_RE = re.compile(r"<(/?)(testcase|skipped|failure|error)\b([^>]*)>")
_STATUS_PRIORITY = {"error": 0, "failed": 1, "skipped": 2, "passed": 3}

# What makes an XML a TEST REPORT. Mirrors the in-container `is_report_file`
# of physical_validator (surefire / failsafe / gradle test-results / pytest
# junit); the two must agree or a receipt would claim files the validator
# never scans — or miss files it does.
REPORT_PATH_MARKERS = (
    "/target/surefire-reports/",
    "/target/failsafe-reports/",
    "/build/test-results/",
    "/.setup_agent/pytest-reports/",
)

_SEQUENCE = itertools.count(1)
_SEQUENCE_LOCK = threading.Lock()


def next_sequence() -> int:
    """Process-global monotonic sequence — receipt ids cannot collide."""
    with _SEQUENCE_LOCK:
        return next(_SEQUENCE)


def next_receipt_id(scope: str, attempt: Any) -> str:
    """`inv-<phase-or-tool>-<attempt-or-seq>-<seq>` (plan §schema v1)."""
    return f"inv-{_slug(scope) or 'runner'}-{_slug(attempt) or '1'}-{next_sequence():04d}"


def snapshot_reports(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    scan_roots: Iterable[str],
) -> Dict[str, str]:
    """Content hashes of every report XML under `scan_roots`, path -> sha256.

    ONE shell round-trip per side of an invocation: `find` filters the report
    shapes itself (no xargs, no second `cat` pass) and hashes what it kept.
    A transport failure yields an empty snapshot rather than an exception —
    an unmeasurable delta must not break the build the model asked for.
    """
    roots = _unique_roots(scan_roots)
    if not roots:
        return {}
    predicates = " -o ".join(
        f"-path {shlex.quote(f'*{marker}*.xml')}" for marker in REPORT_PATH_MARKERS
    )
    command = (
        "find "
        + " ".join(shlex.quote(root) for root in roots)
        + f" -type f \\( {predicates} \\) -exec sha256sum {{}} + 2>/dev/null"
    )
    try:
        result = execute(command) or {}
    except Exception as exc:  # evidence collection never breaks the runner
        logger.debug(f"report snapshot skipped: {exc}")
        return {}
    return _parse_sha256sum(result.get("output") or "")


def report_delta(
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> Dict[str, List[Dict[str, str]]]:
    """What THIS invocation wrote: reports that appeared or changed content.

    Unchanged files never appear — a byte-identical XML from an earlier
    attempt is not this invocation's evidence. Both keys are always present:
    an empty list is the stated fact "this invocation wrote no new/changed
    reports", which is exactly what the primary rollup needs to hear.
    """
    new: List[Dict[str, str]] = []
    changed: List[Dict[str, str]] = []
    for path in sorted(after):
        digest = after[path]
        if path not in before:
            new.append({"path": path, "sha256": digest})
        elif before[path] != digest:
            changed.append({"path": path, "sha256": digest})
    return {"new": new, "changed": changed}


def target_sha(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    working_directory: str,
) -> Optional[str]:
    """The checkout SHA of the tree this invocation ran in, or None.

    Same probe python_tool._target_sha uses for the native-smoke capability
    receipt, with one extra rule: the answer must LOOK like a git object name.
    A container that replies to every command with build-log text has stated
    no sha, and recording that text as provenance would be a fabrication.
    """
    directory = str(working_directory or "").strip()
    if not directory:
        return None
    try:
        result = execute(f"git -C {shlex.quote(directory)} rev-parse HEAD") or {}
    except Exception as exc:  # evidence collection never breaks the runner
        logger.debug(f"target sha unavailable: {exc}")
        return None
    if not _succeeded(result):
        return None
    candidate = _first_line(result.get("output"))
    return candidate if _OBJECT_NAME_RE.fullmatch(candidate) else None


def survey_pins(manifest: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """The survey/config fingerprints AS THE SURVEY RECORDED THEM.

    Read-through only, and only from a manifest the caller ALREADY holds: the
    survey handoff manifest is read exactly once per build by the layer that
    owns the pre-flight, and a receipt must not turn that into a second probe
    (tests/test_build_tool_preflight_integration.py). The survey stamp is the
    single existing producer of these pins, so a pin it does not carry stays an
    absent key — Stage A introduces the survey/document-map fingerprint, and
    nothing is invented in the meantime.
    """
    stamp = manifest.get("survey") if isinstance(manifest, Mapping) else None
    pins: Dict[str, str] = {}
    for key in ("survey_fingerprint", "config_fingerprint"):
        value = stamp.get(key) if isinstance(stamp, Mapping) else None
        if value is None and isinstance(manifest, Mapping):
            value = manifest.get(key)
        text = str(value or "").strip()
        if text:
            pins[key] = text
    return pins


def nearest_domain_root(
    manifest: Optional[Mapping[str, Any]],
    working_directory: str,
) -> Optional[str]:
    """The surveyed build domain this invocation belongs to, or None.

    One invocation belongs to ONE domain: the NEAREST containing root wins, the
    same rule the phase gate's domain rollup applies. A run outside every
    surveyed domain — and a project with no surveyed domains at all — has no
    domain fact, so the key stays absent.
    """
    directory = _normalized_root(working_directory)
    if not directory:
        return None
    containing = [
        root
        for root in _surveyed_domain_roots(manifest)
        if directory == root or directory.startswith(f"{root}/")
    ]
    return max(containing, key=len) if containing else None


def toolchain_fingerprint(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    *,
    executable: Optional[str],
    version_flag: str,
    working_directory: Optional[str] = None,
) -> Optional[Dict[str, str]]:
    """Which runner binary this invocation actually launched, and its version.

    ONE round trip: the resolved path (`command -v`) and the FIRST line of the
    runner's own version output, separated by a marker so an unresolved path
    can never shift into the version slot. A wrapper (`./gradlew`, `./mvnw`) is
    resolved from the invocation's own cwd, which is where it ran.
    """
    runner = str(executable or "").strip()
    if not runner:
        return None
    directory = str(working_directory or "").strip()
    prefix = f"cd {shlex.quote(directory)} 2>/dev/null; " if directory else ""
    command = (
        f"{prefix}command -v {shlex.quote(runner)} 2>/dev/null; "
        f"echo {shlex.quote(TOOLCHAIN_MARKER)}; "
        f"{shlex.quote(runner)} {version_flag} 2>&1 | head -n 1"
    )
    try:
        result = execute(command) or {}
    except Exception as exc:
        logger.debug(f"toolchain fingerprint unavailable: {exc}")
        return None
    resolved, _, version = str(result.get("output") or "").partition(TOOLCHAIN_MARKER)
    fingerprint: Dict[str, str] = {}
    path = _first_line(resolved)
    if path:
        fingerprint["executable"] = path
    line = _first_line(version)[:VERSION_LINE_MAX_CHARS]
    if line:
        fingerprint["version"] = line
    return fingerprint or None


def output_content_hash(output: Optional[str]) -> Optional[str]:
    """sha256 of the output the tool already holds; None when there is none."""
    if output is None:
        return None
    return hashlib.sha256(str(output).encode("utf-8", "replace")).hexdigest()


def read_testcase_outcomes(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    delta: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    """Bounded per-testcase outcomes from THIS invocation's own reports.

    Review binding note (b): `{node_id, status, reason?}` per node, capped at
    ``TESTCASE_OUTCOME_CAP`` with the truncation recorded. Failures and errors
    sort first, so a truncated list still carries the diagnostic signal and the
    order never depends on which file the container listed first.

    Only the reports named by the delta are read — never a tree scan — and only
    their TAGS cross the transport. A report nobody could read is UNKNOWN, not
    "this invocation ran no tests", so the key stays absent entirely.
    """
    paths: List[str] = []
    for bucket in ("new", "changed"):
        for entry in (delta or {}).get(bucket) or ():
            path = str((entry or {}).get("path") or "").strip()
            if path and path not in paths:
                paths.append(path)
    if not paths:
        return None
    truncated = len(paths) > TESTCASE_FILE_CAP
    command = "; ".join(
        f"grep -oE {shlex.quote(TESTCASE_TAG_PATTERN)} {shlex.quote(path)} 2>/dev/null "
        f"| head -n {TESTCASE_TAG_CAP}"
        for path in paths[:TESTCASE_FILE_CAP]
    )
    try:
        # `grep` exits nonzero when a report simply has no matching tag, so the
        # command status says nothing here; only the tokens do.
        result = execute(command) or {}
    except Exception as exc:
        logger.debug(f"testcase outcomes unavailable: {exc}")
        return None
    nodes, seen = _parse_testcase_tags(str(result.get("output") or ""))
    if not nodes:
        return None
    if seen > TESTCASE_OUTCOME_CAP:
        truncated = True
    outcomes: Dict[str, Any] = {"nodes": nodes[:TESTCASE_OUTCOME_CAP]}
    if truncated:
        outcomes["truncated"] = True
    return outcomes


def build_receipt(
    *,
    receipt_id: str,
    tool: str,
    requested_action: str,
    effective_action: str,
    argv: str,
    working_directory: str,
    exit_code: Optional[int],
    before: Mapping[str, str],
    after: Mapping[str, str],
    target_sha: Optional[str] = None,
    survey_fingerprint: Optional[str] = None,
    config_fingerprint: Optional[str] = None,
    domain_id: Optional[str] = None,
    actual_cwd: Optional[str] = None,
    toolchain_fingerprint: Optional[Mapping[str, str]] = None,
    output_content_hash: Optional[str] = None,
    testcase_outcomes: Optional[Mapping[str, Any]] = None,
    contract_id: Optional[str] = None,
    contract_hash: Optional[str] = None,
    compliance: Optional[str] = None,
    capability_observations: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Assemble a schema-v2 receipt. Absent facts serialize as absent keys.

    The v1 block below is frozen: names, shapes and order stay exactly as
    Plan 5 wrote them, because the validator and the phase gate read them.
    """
    receipt: Dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_id": receipt_id,
        "tool": tool,
        "requested_action": requested_action,
        "effective_action": effective_action,
        "argv": argv,
        "working_directory": working_directory,
    }
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        receipt["exit_code"] = exit_code
    receipt["outcome"] = "completed" if exit_code == 0 else "failed"
    receipt["report_delta"] = report_delta(before, after)
    # v2 (spec §C4). `actual_cwd` is the directory the dispatch physically
    # used; `contract_id`/`contract_hash` bind this receipt to the contract the
    # facade froze BEFORE the dispatch, and `compliance` is that comparison's
    # verdict (invocation_contracts.compliance_class). A dispatch with no
    # frozen contract states none of the three — a receipt never claims
    # compliance with a contract that does not exist.
    for key, value in (
        ("actual_cwd", actual_cwd or working_directory),
        ("contract_id", contract_id),
        ("contract_hash", contract_hash),
        ("compliance", compliance),
        ("target_sha", target_sha),
        ("survey_fingerprint", survey_fingerprint),
        ("config_fingerprint", config_fingerprint),
        ("domain_id", domain_id),
        ("output_content_hash", output_content_hash),
    ):
        text = str(value).strip() if value is not None else ""
        if text:
            receipt[key] = text
    if toolchain_fingerprint:
        receipt["toolchain_fingerprint"] = dict(toolchain_fingerprint)
    if testcase_outcomes:
        receipt["testcase_outcomes"] = dict(testcase_outcomes)
    # Spec §C8: what a PHYSICAL probe observed about a resolved capability.
    # A dispatch that probed nothing states nothing — the key is absent, never
    # an empty list, because "no capability was probed" and "a probe found
    # nothing" are different facts.
    observations = [
        {str(key): str(value) for key, value in dict(entry).items()}
        for entry in capability_observations or ()
        if isinstance(entry, Mapping) and entry
    ]
    if observations:
        receipt["capability_observations"] = observations
    return receipt


def write_receipt(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    receipt: Mapping[str, Any],
) -> bool:
    """Persist one receipt atomically (temp file + `mv`). False on failure.

    Never raises: the caller is mid-invocation and owes the model a result.
    """
    receipt_id = str((receipt or {}).get("receipt_id") or "").strip()
    if not receipt_id:
        return False
    try:
        body = json.dumps(receipt, sort_keys=True)
    except (TypeError, ValueError) as exc:
        logger.debug(f"invocation receipt {receipt_id} is not serializable: {exc}")
        return False
    final = f"{RECEIPT_DIR}/{receipt_id}.json"
    temp = f"{final}.tmp"
    command = (
        f"mkdir -p {shlex.quote(RECEIPT_DIR)} && "
        f"cat > {shlex.quote(temp)} <<'{RECEIPT_HEREDOC}' && "
        f"mv -f {shlex.quote(temp)} {shlex.quote(final)}\n"
        f"{body}\n{RECEIPT_HEREDOC}"
    )
    try:
        result = execute(command) or {}
    except Exception as exc:
        logger.debug(f"invocation receipt {receipt_id} not persisted: {exc}")
        return False
    return _succeeded(result)


def record_invocation(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    *,
    tool: str,
    attempt: Any,
    requested_action: str,
    effective_action: str,
    argv: str,
    working_directory: str,
    exit_code: Optional[int],
    before: Mapping[str, str],
    after: Mapping[str, str],
    output: Optional[str] = None,
    requirements: Optional[Mapping[str, Any]] = None,
    contract_id: Optional[str] = None,
    contract_hash: Optional[str] = None,
    compliance: Optional[str] = None,
    capability_observations: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Persist the receipt for one runner call; return its ToolResult metadata.

    The v2 facts are collected HERE, after the dispatch the caller already
    completed: the tree's sha, the runner's toolchain and the per-testcase
    outcomes of the reports this invocation wrote are probed; the survey pins
    and the domain are read from the manifest the CALLER already holds, never
    from a second manifest probe. The contract binding
    (`contract_id`/`contract_hash`/`compliance`) is passed in by the caller,
    which is the only layer that knows both the frozen contract and the argv it
    physically ran. Every fact degrades to an absent key — none of them can
    fail the build the model is waiting for.

    Byte-compat (Plans 2-4 pattern): a persisted receipt adds ONLY
    `receipt_id`; a failed write adds ONLY `receipt_persisted: false`.
    """
    receipt = build_receipt(
        receipt_id=next_receipt_id(tool, attempt),
        tool=tool,
        requested_action=requested_action,
        effective_action=effective_action,
        argv=argv,
        working_directory=working_directory,
        exit_code=exit_code,
        before=before,
        after=after,
        target_sha=target_sha(execute, working_directory),
        domain_id=nearest_domain_root(requirements, working_directory),
        actual_cwd=working_directory,
        toolchain_fingerprint=toolchain_fingerprint(
            execute,
            executable=runner_executable(argv, tool),
            version_flag=VERSION_FLAGS.get(tool, "--version"),
            working_directory=working_directory,
        ),
        output_content_hash=output_content_hash(output),
        testcase_outcomes=read_testcase_outcomes(execute, report_delta(before, after)),
        contract_id=contract_id,
        contract_hash=contract_hash,
        compliance=compliance,
        capability_observations=capability_observations,
        **survey_pins(requirements),
    )
    if write_receipt(execute, receipt):
        return {"receipt_id": receipt["receipt_id"]}
    return {"receipt_persisted": False}


def runner_executable(argv: str, tool: Optional[str] = None) -> Optional[str]:
    """The binary the argv actually launches (`./gradlew`, a venv python, mvn)."""
    text = str(argv or "").strip()
    if text:
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()
        if tokens:
            return tokens[0]
    return RUNNER_DEFAULTS.get(str(tool or ""))


def _unique_roots(scan_roots: Iterable[str]) -> List[str]:
    roots: List[str] = []
    for raw in scan_roots or ():
        root = str(raw or "").strip()
        if not root:
            continue
        root = root.rstrip("/") or "/"
        if root not in roots:
            roots.append(root)
    return roots


def _parse_sha256sum(output: str) -> Dict[str, str]:
    """`<hash>  <path>` lines; anything else (stderr noise) is ignored."""
    snapshot: Dict[str, str] = {}
    for line in (output or "").splitlines():
        digest, separator, path = line.partition("  ")
        if not separator:
            continue
        # GNU sha256sum escapes newline/backslash filenames with a leading '\'.
        digest = digest.strip().lstrip("\\")
        path = path.strip()
        if not path or len(digest) != 64:
            continue
        try:
            int(digest, 16)
        except ValueError:
            continue
        snapshot[path] = digest
    return snapshot


def _parse_testcase_tags(output: str) -> Tuple[List[Dict[str, str]], int]:
    """The container's tag token stream -> (sorted nodes, nodes seen).

    A tag stream is enough: JUnit puts the outcome in the testcase's child tag
    and the skip reason in that child's `message` attribute, so the report
    bodies never have to cross the transport. A node whose closing tag was cut
    by the per-file bound still closes when the next testcase opens — a
    truncated read reports fewer nodes, never a wrong one.
    """
    nodes: Dict[str, Dict[str, str]] = {}
    pending: Optional[Dict[str, str]] = None

    def close(node: Optional[Dict[str, str]]) -> None:
        if node and node["node_id"] not in nodes:
            nodes[node["node_id"]] = node

    for match in _TESTCASE_TAG_RE.finditer(output or ""):
        closing, tag, attributes = match.group(1), match.group(2), match.group(3)
        self_closing = attributes.rstrip().endswith("/")
        if tag == "testcase":
            close(pending)
            pending = None
            if closing:
                continue
            node_id = _testcase_node_id(attributes)
            if not node_id:
                continue
            pending = {"node_id": node_id, "status": "passed"}
            if self_closing:
                close(pending)
                pending = None
            if len(nodes) >= TESTCASE_PARSE_CAP:
                break
        elif pending is not None:
            if tag == "skipped":
                pending["status"] = "skipped"
                reason = _tag_attribute(attributes, "message")
                if reason:
                    pending["reason"] = reason[:SKIP_REASON_MAX_CHARS]
            elif tag == "failure":
                pending["status"] = "failed"
            elif tag == "error":
                pending["status"] = "error"
    close(pending)
    ordered = sorted(
        nodes.values(),
        key=lambda node: (_STATUS_PRIORITY.get(node["status"], 9), node["node_id"]),
    )
    return ordered, len(ordered)


def _testcase_node_id(attributes: str) -> str:
    """`<classname>#<name>`, or the bare name when the report has no class."""
    name = _tag_attribute(attributes, "name")
    if not name:
        return ""
    classname = _tag_attribute(attributes, "classname")
    return f"{classname}#{name}" if classname else name


def _tag_attribute(attributes: str, name: str) -> str:
    match = re.search(rf"""\b{name}=(?:"([^"]*)"|'([^']*)')""", attributes or "")
    if not match:
        return ""
    return " ".join(html.unescape(match.group(1) or match.group(2) or "").split())


def _surveyed_domain_roots(manifest: Optional[Mapping[str, Any]]) -> List[str]:
    """``build_domains`` roots, read the way every other rec fact is read.

    The manifest projects the recommendation's keys at top level; a manifest
    written before that projection existed carries only the nested
    ``build_recommendation`` (same dual read the phase gate performs).
    """
    if not isinstance(manifest, Mapping):
        return []
    raw = manifest.get("build_domains")
    if raw is None:
        recommendation = manifest.get("build_recommendation")
        if isinstance(recommendation, Mapping):
            raw = recommendation.get("build_domains")
    if not isinstance(raw, (list, tuple)):
        return []
    roots: List[str] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        root = _normalized_root(item.get("root"))
        if root and root not in roots:
            roots.append(root)
    return roots


def _normalized_root(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return posixpath.normpath(raw).rstrip("/") or "/"


def _first_line(output: Any) -> str:
    for line in str(output or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _succeeded(result: Mapping[str, Any]) -> bool:
    """Container results state either `success` or an exit code; accept both."""
    success = (result or {}).get("success")
    if success is None:
        success = (result or {}).get("exit_code") == 0
    return bool(success)


def _slug(value: Any) -> str:
    return "".join(
        character if character.isalnum() else "_" for character in str(value or "").strip()
    ).strip("_")

"""Seal module-qualified testcase rows inside an invocation receipt.

The report scanner can prove that an XML belongs to one invocation, but its
aggregate deliberately loses the report/receipt boundary.  Metrics-v2 needs
that boundary to distinguish a test definition from a physical execution.
This module parses only a receipt's content-hash delta, verifies the bytes it
reads against that delta, and enriches every runtime testcase with identity
facts that are already present on the receipt.

No field is inferred from a project name or from a later tree scan.  When a
runner-native module coordinate cannot be proved, the entire envelope is
``unavailable``; consumers must not publish a partial subject/case rollup.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shlex
from typing import Any, Callable, Mapping, Optional, Sequence

from loguru import logger

from sag.testcases.results import canonical_test_identity, normalize_test_status
from sag.utils.container_io import write_container_text_atomic

ROW_ENVELOPE_VERSION = 2
ROW_INPUT_DIR = "/workspace/.setup_agent/.testcase-row-input"
_REPORT_BUCKETS = ("new", "changed", "cached")
_OUTCOME_SEVERITY = {"skipped": 0, "passed": 1, "failed": 2, "error": 3}
_DIAGNOSTIC_ORDER = {"error": 0, "failed": 1, "skipped": 2, "passed": 3}
_DIAGNOSTIC_CAP = 50
_REASON_CAP = 200
_RECEIPT_SEQUENCE_RE = re.compile(r"-(\d+)$")
_HEX_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ROW_OUTCOMES = frozenset(_OUTCOME_SEVERITY)
_RED_OUTCOMES = frozenset({"failed", "error"})

# --- the universal row bounds (P-A) ----------------------------------------
#
# These hold for EVERY runner, because the receipt they feed is one schema with
# one canonical byte budget. The measured case they exist for: kafka's
# 2026-08-26 session left 27,219 executed tests in 1,176 reports, and an
# unbounded exact read sealed all of them into `testcase_execution_rows` — ~16
# MB of identity against a 16 MB canonical budget, which does not degrade the
# sample, it VOIDS the receipt at write time and takes the exit code, the argv
# and the report delta with it.
#
# Nothing unbounded may enter a receipt. Identities are a bounded sample;
# totals (the suite-summary tier) stay complete; every bound below records what
# it dropped, and a red row is never dropped while a green survives anywhere.
#
# The total cap is `invocation_receipts.GRADLE_TESTCASE_ROW_CAP` and the
# per-file cap is its `TESTCASE_TAG_CAP` — the harvest's own bounds, restated
# here because this module may not import the receipt module that imports it.
# `invocation_receipts` asserts the alignment at import time.
DELTA_TESTCASE_ROW_CAP = 2048
# Not a discard: the per-file bound decides which rows get the sample's FIRST
# slots, so no single report can crowd a reactor's other files out of it. A
# file's surplus rows are demoted, not thrown away, and are kept whenever the
# total cap still has room — one pytest report holding 1,000 tests is one file,
# and cutting it to 400 would drop rows the budget could carry.
DELTA_PER_FILE_ROW_CAP = 400
# One parsed row's transport size. A measured kafka identity canonicalizes to
# roughly 500 bytes; a row twice that is a pathological display name, and it is
# DROPPED (and counted) rather than clipped — a clipped identity is a different
# test, and a receipt may not invent one.
DELTA_ROW_MAX_JSON_BYTES = 1024
# The largest report this transport will PARSE. kafka's streams report is
# 137.8 MB of `system-out` around its testcases; a DOM over it costs the
# container an order of magnitude more, for rows that cannot fit the sample
# anyway. Its digest is still streamed and verified — the file is disclosed as
# a dropped file (P-B: what we counted is bound to bytes we read), never as an
# absence and never as a hash failure.
DELTA_REPORT_MAX_BYTES = 8 * 1024 * 1024
# What sealing adds to one parsed row: the receipt-scoped identity material
# (run/receipt/target/domain/module/framework/disposition/execution_id). Stated
# as a bound so the rows section's worst case is a constant relation rather
# than a hope; `invocation_receipts` asserts it against the canonical budget
# and `tests/test_test_execution_rows.py` measures real sealed rows against it.
SEALED_ROW_OVERHEAD_MAX_BYTES = 768
_REPORT_READ_CHUNK_BYTES = 1 << 20
_ROW_BOUND_FIELDS = (
    "observed_rows",
    "kept_rows",
    "dropped_red",
    "dropped_green",
    "dropped_files",
    "per_file_cap_drops",
    "total_cap_drops",
    "oversize_row_drops",
    "unparsed_reports",
)


class TestcaseRowContractError(ValueError):
    """A row cannot enter the module-qualified metrics-v2 claim set."""


# The input path is the sole argv value.  Report paths therefore never enter a
# shell command, and a reactor with thousands of reports cannot hit ARG_MAX.
#
# The caps arrive as literals through `_row_parser_program`, exactly as the
# Gradle head reader takes its `HEAD_BYTES`: the program that runs in the
# container is the program whose bounds this module states.
_CONTAINER_REPORT_ROW_PARSER = r"""
import json
import sys
import xml.etree.ElementTree as ET
from hashlib import sha256

PER_FILE_ROW_CAP = ROW_CAP_PER_FILE
TOTAL_ROW_CAP = ROW_CAP_TOTAL
ROW_MAX_JSON_BYTES = ROW_CAP_JSON_BYTES
REPORT_MAX_BYTES = ROW_CAP_REPORT_BYTES
REASON_MAX_CHARS = ROW_CAP_REASON_CHARS
READ_CHUNK_BYTES = ROW_CAP_CHUNK_BYTES
RED = ("failed", "error")


def local_name(element):
    return element.tag.rsplit("}", 1)[-1]


def outcome(testcase):
    children = {local_name(child) for child in testcase}
    if "error" in children:
        return "error"
    if "failure" in children:
        return "failed"
    if "skipped" in children or testcase.get("status") == "skipped":
        return "skipped"
    return "passed"


def reason(testcase, status):
    wanted = {"error": "error", "failed": "failure", "skipped": "skipped"}.get(status)
    if not wanted:
        return None
    for child in testcase:
        if local_name(child) == wanted:
            # Collapsed and clipped to the exact bound the receipt's own
            # diagnostic list clips to, so nothing the receipt would have
            # carried is lost here and a megabyte-long assertion message
            # cannot ride a row into the canonical budget.
            value = " ".join((child.get("message") or "").split())[:REASON_MAX_CHARS]
            return value or None
    return None


def collection_node(testcase):
    if (testcase.get("classname") or "").strip():
        return False
    for child in testcase:
        if local_name(child) not in ("error", "skipped"):
            continue
        if "collection" in (child.get("message") or "").lower():
            return True
    return False


def declared_count(root):
    # Prefer the document root's aggregate.  Otherwise sum only OUTERMOST
    # suites: nested suite counts include their children and summing every
    # testsuite would double count them.
    try:
        value = root.get("tests")
        if value is not None:
            return max(0, int(value))
    except (TypeError, ValueError):
        return None
    outer = [child for child in root if local_name(child) == "testsuite"]
    if not outer and local_name(root) == "testsuite":
        outer = [root]
    values = []
    for suite in outer:
        value = suite.get("tests")
        if value is None:
            return None
        try:
            values.append(max(0, int(value)))
        except (TypeError, ValueError):
            return None
    return sum(values) if values else None


def row_bytes(row):
    return len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


# The file's digest always; its bytes only when they are parseable here.
# Streamed, so a 137.8 MB report costs one chunk of memory to hash, and the
# digest is over the WHOLE file either way: a count is only bound to bytes
# that were all read.
def digest_and_body(path):
    hasher = sha256()
    chunks = []
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
            if size <= REPORT_MAX_BYTES:
                chunks.append(chunk)
            else:
                del chunks[:]
    if size > REPORT_MAX_BYTES:
        return hasher.hexdigest(), None
    return hasher.hexdigest(), b"".join(chunks)


bounds = {
    "observed_rows": 0,
    "kept_rows": 0,
    "dropped_red": 0,
    "dropped_green": 0,
    "dropped_files": 0,
    "per_file_cap_drops": 0,
    "total_cap_drops": 0,
    "oversize_row_drops": 0,
    "unparsed_reports": 0,
}
result = {"status": "complete", "report_count": 0, "rows": [], "reasons": [], "bounds": bounds}
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        reports = json.load(handle)
except Exception as exc:
    print(json.dumps({"status": "unavailable", "rows": [], "reasons": ["input_unreadable"]}))
    raise SystemExit(0)

# Four buckets in priority order. The per-file cap does not DISCARD a file's
# surplus rows, it demotes them: a run that fits the sample keeps every row it
# read (one pytest report is one file, and clipping it to the per-file cap
# would throw away rows the budget could hold), while a reactor with 1,176
# reports still spends its first slots on the widest spread of files it can.
# Reds are ahead of every green in both tiers, which is the one law a bound
# here may not break.
primary_red = []
spill_red = []
primary_green = []
spill_green = []
# Files that produced at least one runtime testcase, and files whose
# identities this read never built at all. Both are losses when the sample
# ends up carrying no row from them; neither may go unstated.
row_files = set()
unparsed_files = set()


def drop(status, cap):
    bounds[cap] += 1
    bounds["dropped_red" if status in RED else "dropped_green"] += 1


for report in reports:
    path = report.get("path")
    expected = str(report.get("sha256") or "").lower()
    try:
        digest, body = digest_and_body(path)
    except Exception:
        result["status"] = "unavailable"
        result["reasons"].append("report_unreadable")
        continue
    if digest != expected:
        result["status"] = "unavailable"
        result["reasons"].append("report_hash_mismatch")
        continue
    # Read and proved to be this receipt's bytes. What a CAP does with it
    # afterwards is a disclosure, never an unavailability: a bound that
    # emptied the envelope would be the burst it exists to prevent.
    result["report_count"] += 1
    if body is None:
        bounds["unparsed_reports"] += 1
        unparsed_files.add(path)
        continue
    try:
        root = ET.fromstring(body)
    except Exception:
        result["status"] = "unavailable"
        result["reasons"].append("report_unreadable")
        continue
    testcases = [element for element in root.iter() if local_name(element) == "testcase"]
    runtime = [element for element in testcases if not collection_node(element)]
    declared = declared_count(root)
    if declared is not None and declared != len(testcases):
        result["status"] = "unavailable"
        result["reasons"].append("declared_testcase_count_mismatch")
        continue
    suite_files = {}

    def map_suite_files(element, inherited=None):
        nearest = inherited
        if local_name(element) == "testsuite":
            nearest = element.get("file") or inherited
        if local_name(element) == "testcase":
            suite_files[id(element)] = nearest
        for child in element:
            map_suite_files(child, nearest)

    map_suite_files(root)
    if runtime:
        row_files.add(path)
    bounds["observed_rows"] += len(runtime)
    file_red = []
    file_green = []
    for testcase_ordinal, testcase in enumerate(runtime, 1):
        status = outcome(testcase)
        row = {
            "report_path": path,
            "report_sha256": expected,
            "classname": (testcase.get("classname") or "").strip(),
            "name": (testcase.get("name") or "").strip(),
            "source_file": testcase.get("file") or suite_files.get(id(testcase)),
            "outcome": status,
            "reason": reason(testcase, status),
            "execution_ordinal": testcase_ordinal,
        }
        if row_bytes(row) > ROW_MAX_JSON_BYTES:
            drop(status, "oversize_row_drops")
            continue
        (file_red if status in RED else file_green).append(row)
    # Red first, inside the file and then across them: one report's 30,000
    # green tests may not spend the whole sample, and no green anywhere
    # outranks a red.
    ordered = file_red + file_green
    for row in ordered[:PER_FILE_ROW_CAP]:
        target = primary_red if row["outcome"] in RED else primary_green
        if len(target) < TOTAL_ROW_CAP:
            target.append(row)
        else:
            drop(row["outcome"], "total_cap_drops")
    for row in ordered[PER_FILE_ROW_CAP:]:
        target = spill_red if row["outcome"] in RED else spill_green
        if len(target) < TOTAL_ROW_CAP:
            target.append(row)
        else:
            drop(row["outcome"], "per_file_cap_drops")

kept = []
# The cap a dropped row answers to: rows the per-file bound demoted lose their
# slot to it, everything else to the total bound.
for bucket, cap in (
    (primary_red, "total_cap_drops"),
    (spill_red, "per_file_cap_drops"),
    (primary_green, "total_cap_drops"),
    (spill_green, "per_file_cap_drops"),
):
    room = TOTAL_ROW_CAP - len(kept)
    kept.extend(bucket[:room])
    for row in bucket[room:]:
        drop(row["outcome"], cap)
kept_files = set()
for row in kept:
    kept_files.add(row["report_path"])
bounds["kept_rows"] = len(kept)
bounds["dropped_files"] = len(row_files - kept_files) + len(unparsed_files)
result["rows"] = kept
result["reasons"] = sorted(set(result["reasons"]))
print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
"""


def _delta_reports(delta: Mapping[str, Any]) -> tuple[list[dict[str, str]], bool]:
    reports: dict[str, str] = {}
    conflicts: set[str] = set()
    for bucket in _REPORT_BUCKETS:
        for entry in delta.get(bucket) or ():
            if not isinstance(entry, Mapping):
                continue
            path = str(entry.get("path") or "").strip()
            digest = str(entry.get("sha256") or "").strip().lower()
            if not path or not re.fullmatch(r"[0-9a-f]{64}", digest):
                continue
            previous = reports.get(path)
            if previous is not None and previous != digest:
                conflicts.add(path)
            reports[path] = digest
    selected = [
        {"path": path, "sha256": reports[path]} for path in sorted(reports) if path not in conflicts
    ]
    return selected, bool(conflicts)


def _row_parser_program() -> str:
    """The container program with this module's bounds compiled into it."""

    return (
        _CONTAINER_REPORT_ROW_PARSER.replace("ROW_CAP_PER_FILE", str(DELTA_PER_FILE_ROW_CAP))
        .replace("ROW_CAP_TOTAL", str(DELTA_TESTCASE_ROW_CAP))
        .replace("ROW_CAP_JSON_BYTES", str(DELTA_ROW_MAX_JSON_BYTES))
        .replace("ROW_CAP_REPORT_BYTES", str(DELTA_REPORT_MAX_BYTES))
        .replace("ROW_CAP_REASON_CHARS", str(_REASON_CAP))
        .replace("ROW_CAP_CHUNK_BYTES", str(_REPORT_READ_CHUNK_BYTES))
    )


def _row_json_bytes(row: Mapping[str, Any]) -> int:
    try:
        return len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        # Unserializable is unsealable and unsendable; treat it as over the
        # bound so it is dropped and counted rather than carried.
        return DELTA_ROW_MAX_JSON_BYTES + 1


def _row_is_red(row: Any) -> bool:
    if not isinstance(row, Mapping):
        return False
    return str(row.get("outcome") or "").strip().lower() in _RED_OUTCOMES


def bound_testcase_rows(
    rows: Sequence[Any],
    reported: Mapping[str, Any] | None = None,
) -> tuple[list[Any], dict[str, int]]:
    """Hold the universal bounds host-side, and account for every drop.

    The container program applies the same bounds at the source — that is what
    keeps a 27,219-row read off the transport in the first place. This is the
    guarantee, not the optimization: whatever a parser hands back, what leaves
    here is at most `DELTA_TESTCASE_ROW_CAP` rows of at most
    `DELTA_ROW_MAX_JSON_BYTES` each, red rows first, with kept + dropped ==
    observed at every cap. `reported` is the container's own accounting, which
    counts rows this side never saw; the two are added, never conflated.
    """

    bounds = {field: 0 for field in _ROW_BOUND_FIELDS}
    stated = reported if isinstance(reported, Mapping) else {}
    for field in _ROW_BOUND_FIELDS:
        value = stated.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            bounds[field] = value
    red: list[Any] = []
    green: list[Any] = []
    # A row the parser did not state as an object can never be sealed, but it
    # is still a row: it sorts last, and the cap may take it like any other.
    unclassified: list[Any] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            unclassified.append(raw)
            continue
        if _row_json_bytes(raw) > DELTA_ROW_MAX_JSON_BYTES:
            bounds["oversize_row_drops"] += 1
            bounds["dropped_red" if _row_is_red(raw) else "dropped_green"] += 1
            continue
        (red if _row_is_red(raw) else green).append(raw)
    ordered = [*red, *green, *unclassified]
    kept = ordered[:DELTA_TESTCASE_ROW_CAP]
    for raw in ordered[len(kept) :]:
        bounds["total_cap_drops"] += 1
        bounds["dropped_red" if _row_is_red(raw) else "dropped_green"] += 1
    covered = {str(row.get("report_path") or "") for row in ordered if isinstance(row, Mapping)}
    carried = {str(row.get("report_path") or "") for row in kept if isinstance(row, Mapping)}
    bounds["dropped_files"] += len((covered - carried) - {""})
    bounds["kept_rows"] = len(kept)
    # What the whole read saw: the container's own observation when it made
    # one (it saw the rows this side never received), else what arrived.
    bounds["observed_rows"] = max(bounds["observed_rows"], len(rows))
    return kept, bounds


def verify_row_bound_arithmetic(bounds: Mapping[str, Any]) -> None:
    """kept + dropped == observed, and every drop answers to exactly one cap.

    The accounting a disclosure is built from is HALF the container's (it saw
    rows this side never received) and half this side's, so it is only exact
    while both halves add up. A parser that stated 27,219 observed, 3 kept and
    12 dropped is not a bound that fired: it is an accounting that cannot be
    true, and a disclosure built from it would state a loss no read ever made.
    That sample is refused whole — the totals tier is a different measurement
    and keeps its numbers, so nothing load-bearing rides on this (P-A).
    """

    def count(field: str) -> int:
        value = bounds.get(field) if isinstance(bounds, Mapping) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TestcaseRowContractError(f"row bound {field} is not a count")
        return value

    dropped = count("dropped_red") + count("dropped_green")
    if count("kept_rows") + dropped != count("observed_rows"):
        raise TestcaseRowContractError("row bounds do not account for every observed row")
    # `dropped_red`/`dropped_green` say WHAT was lost; the three cap counters
    # say WHY. A loss with no cause is the silent cap wearing a disclosure's
    # clothes, and a cause with no loss is a bound that fired on nothing.
    capped = count("oversize_row_drops") + count("per_file_cap_drops") + count("total_cap_drops")
    if capped != dropped:
        raise TestcaseRowContractError("row bounds drop rows no cap accounts for")


def read_delta_testcase_rows(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    *,
    receipt_id: str,
    delta: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    """Read a BOUNDED identity sample from this receipt's hash-bound reports.

    Every claimed report is read whole and its digest verified against the
    delta's claim in the same read; what comes back is at most
    `DELTA_TESTCASE_ROW_CAP` rows, red first, with `row_bounds` stating exactly
    what the caps dropped. The counts a consumer needs are the suite-total
    tier's, and they stay complete however hard these bounds bite.

    The temporary input is written through the same bounded atomic transport as
    durable evidence.  Its exact, receipt-scoped path is removed after the one
    parser call; failure at any step returns an unavailable envelope and never
    changes the runner result.
    """

    reports, delta_conflict = _delta_reports(delta)
    if delta_conflict:
        return {
            "schema_version": ROW_ENVELOPE_VERSION,
            "status": "unavailable",
            "rows": [],
            "reasons": ["report_delta_conflict"],
        }
    if not reports:
        return None
    slug = "".join(character if character.isalnum() else "_" for character in receipt_id)
    input_path = f"{ROW_INPUT_DIR}/{slug or 'receipt'}.json"
    body = json.dumps(reports, ensure_ascii=False, separators=(",", ":"))
    persisted = write_container_text_atomic(execute, input_path, body, validate_json=True)
    if not persisted.persisted:
        return {
            "schema_version": ROW_ENVELOPE_VERSION,
            "status": "unavailable",
            "rows": [],
            "reasons": ["row_input_persistence_failed"],
        }
    command = f"python3 -c {shlex.quote(_row_parser_program())} {shlex.quote(input_path)}"
    try:
        try:
            result = execute(command, truncate_output=False) or {}
        except TypeError as exc:
            if "truncate_output" not in str(exc):
                raise
            result = execute(command) or {}
        if (
            not isinstance(result, Mapping)
            or result.get("success") is False
            or result.get("dispatch_status")
            or result.get("exit_code") not in (None, 0)
        ):
            raise ValueError("row parser transport failed")
        payload = json.loads(str(result.get("output") or ""))
        if not isinstance(payload, Mapping):
            raise ValueError("row parser did not return an object")
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise ValueError("row parser did not return a row list")
        report_count = payload.get("report_count")
        report_count_valid = (
            isinstance(report_count, int)
            and not isinstance(report_count, bool)
            and report_count == len(reports)
        )
        reasons = {str(reason) for reason in payload.get("reasons") or () if str(reason).strip()}
        if not report_count_valid:
            reasons.add("report_count_mismatch")
        # A cap is never a reason: `seal_testcase_execution_rows` turns any
        # reason into an unavailable envelope with no rows at all, and a bound
        # that emptied the sample it was supposed to protect would be the
        # kafka burst by another route. Bounds ride their own field.
        bounded, bounds = bound_testcase_rows(rows, payload.get("bounds"))
        try:
            verify_row_bound_arithmetic(bounds)
        except TestcaseRowContractError as exc:
            logger.debug(f"receipt testcase row bounds do not reconcile for {receipt_id}: {exc}")
            return {
                "schema_version": ROW_ENVELOPE_VERSION,
                "status": "unavailable",
                "rows": [],
                "reasons": ["row_bounds_inconsistent"],
            }
        return {
            "schema_version": ROW_ENVELOPE_VERSION,
            "status": (
                "complete"
                if payload.get("status") == "complete" and report_count_valid
                else "unavailable"
            ),
            "report_count": report_count if report_count_valid else 0,
            "rows": bounded,
            "row_bounds": bounds,
            **({"reasons": sorted(reasons)} if reasons else {}),
        }
    except Exception as exc:
        logger.debug(f"receipt testcase rows unavailable for {receipt_id}: {exc}")
        return {
            "schema_version": ROW_ENVELOPE_VERSION,
            "status": "unavailable",
            "rows": [],
            "reasons": ["row_parser_failed"],
        }
    finally:
        try:
            execute(f"rm -f -- {shlex.quote(input_path)}")
        except Exception:
            pass


def _receipt_sequence(receipt_id: str) -> Optional[int]:
    match = _RECEIPT_SEQUENCE_RE.search(str(receipt_id or "").strip())
    if not match:
        return None
    value = int(match.group(1))
    return value if value >= 1 else None


def rows_were_bounded(parsed: Mapping[str, Any] | None) -> bool:
    """Whether any universal bound took something out of this read."""

    bounds = parsed.get("row_bounds") if isinstance(parsed, Mapping) else None
    if not isinstance(bounds, Mapping):
        return False
    return any(
        isinstance(bounds.get(field), int)
        and not isinstance(bounds.get(field), bool)
        and int(bounds.get(field) or 0) > 0
        for field in ("dropped_red", "dropped_green", "dropped_files", "unparsed_reports")
    )


def diagnostic_testcase_outcomes(
    parsed: Mapping[str, Any] | None,
) -> Optional[dict[str, Any]]:
    """Project the exact parse onto the historical bounded diagnostic list."""

    if not isinstance(parsed, Mapping):
        return None
    nodes: dict[str, dict[str, str]] = {}
    seen = 0
    for raw in parsed.get("rows") or ():
        if not isinstance(raw, Mapping):
            continue
        # Collapse whitespace exactly as `reason` is collapsed four lines down,
        # and for the same reason: a JUnit display name may legally carry a
        # newline (surefire writes it as `&#10;`, and the in-container parser
        # unescapes it), while a receipt identifier may not carry control text.
        # The legacy tag-stream parser this replaced sanitized here; live kafka
        # lost its whole terminal receipt to the omission.
        name = " ".join(str(raw.get("name") or "").split())
        classname = " ".join(str(raw.get("classname") or "").split())
        status = str(raw.get("outcome") or "").strip()
        if not name or status not in _DIAGNOSTIC_ORDER:
            continue
        seen += 1
        node_id = f"{classname}#{name}" if classname else name
        node = {"node_id": node_id, "status": status}
        reason = " ".join(str(raw.get("reason") or "").split())[:_REASON_CAP]
        if reason:
            node["reason"] = reason
        previous = nodes.get(node_id)
        if previous is None or _OUTCOME_SEVERITY[status] > _OUTCOME_SEVERITY[previous["status"]]:
            nodes[node_id] = node
    if not nodes:
        return None
    ordered = sorted(
        nodes.values(),
        key=lambda node: (_DIAGNOSTIC_ORDER[node["status"]], node["node_id"]),
    )
    result: dict[str, Any] = {"nodes": ordered[:_DIAGNOSTIC_CAP]}
    # A list built from a bounded read is a sample even when it fits: the rows
    # this projection never saw are as absent from it as the ones its own cap
    # cut, and `seen` can only count what arrived.
    if seen > _DIAGNOSTIC_CAP or parsed.get("status") != "complete" or rows_were_bounded(parsed):
        result["truncated"] = True
    return result


def _relative_root(root: str, parent: str) -> Optional[str]:
    normalized_root = posixpath.normpath(str(root or ""))
    normalized_parent = posixpath.normpath(str(parent or ""))
    if not normalized_root.startswith("/") or not normalized_parent.startswith("/"):
        return None
    relative = posixpath.relpath(normalized_root, normalized_parent)
    if relative == ".":
        return "."
    if relative == ".." or relative.startswith("../"):
        return None
    return relative


def read_gradle_project_map(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    domain_id: Optional[str],
) -> Optional[dict[str, str]]:
    """Prove Gradle project-path -> projectDir from one static settings file.

    Conventional directory shape alone is not proof: Gradle permits arbitrary
    ``projectDir`` relocation. Dynamic/apply/include-build forms therefore make
    this map unavailable rather than allowing the row sealer to guess.
    """

    root = posixpath.normpath(str(domain_id or ""))
    if not root.startswith("/"):
        return None
    present: list[str] = []
    for name in ("settings.gradle", "settings.gradle.kts"):
        path = f"{root}/{name}"
        try:
            result = (
                execute(
                    f"test -f {shlex.quote(path)} && cat {shlex.quote(path)}", truncate_output=False
                )
                or {}
            )
        except TypeError as exc:
            if "truncate_output" not in str(exc):
                return None
            result = execute(f"test -f {shlex.quote(path)} && cat {shlex.quote(path)}") or {}
        except Exception:
            return None
        if result.get("exit_code") == 0 and not result.get("dispatch_status"):
            present.append(str(result.get("output") or ""))
    if len(present) != 1:
        return None
    try:
        from sag.agent.forced_build_graph import (
            _collect_gradle_calls,
            _gradle_literal_relocations,
            _gradle_project_path,
            _normalized_gradle_project_id,
            _strip_gradle_comments,
        )

        source = _strip_gradle_comments(present[0])
        if re.search(r"\bapply\s*(?:\(|\s)|\bincludeFlat\b", source):
            return None
        included_builds, residual = _collect_gradle_calls(source, "includeBuild")
        if included_builds or re.search(r"\bincludeBuild\b", residual):
            return None
        projects, residual = _collect_gradle_calls(residual, "include")
        if re.search(r"\binclude\b", residual):
            return None
        relocations, _ = _gradle_literal_relocations(source)
        mapping = {root: ":root"}
        for raw_project in projects:
            project_id = _normalized_gradle_project_id(raw_project)
            default = _gradle_project_path(project_id, root)
            raw_path = relocations.pop(project_id, default)
            candidate = posixpath.normpath(
                raw_path if posixpath.isabs(raw_path) else posixpath.join(root, raw_path)
            )
            if candidate != root and not candidate.startswith(root + "/"):
                return None
            if candidate in mapping and mapping[candidate] != project_id:
                return None
            mapping[candidate] = project_id
        if relocations:
            return None
        return mapping
    except Exception:
        # The shared settings parser raises its own typed graph-unavailable
        # signal for every dynamic or ambiguous form. Exact rows treat all of
        # those as absence of proof, never as a runner failure.
        return None


def _report_module_root(tool: str, report_path: str) -> Optional[str]:
    markers = {
        "maven": ("/target/surefire-reports/", "/target/failsafe-reports/"),
        "gradle": ("/build/test-results/",),
    }
    for marker in markers.get(tool, ()):
        prefix, separator, _ = report_path.partition(marker)
        if separator and prefix:
            return posixpath.normpath(prefix)
    return None


def _python_module_identity(
    *,
    source_file: str,
    fallback_module: str,
    domain_id: str,
) -> Optional[str]:
    """Prove one cwd-independent Python module inside the surveyed domain."""

    raw_source = str(source_file or "").strip().replace("\\", "/")
    domain = posixpath.normpath(str(domain_id or ""))
    if raw_source:
        normalized_source = posixpath.normpath(raw_source)
        if normalized_source.startswith("/"):
            module = _relative_root(normalized_source, domain)
        else:
            module = normalized_source
    else:
        module = str(fallback_module or "").strip().replace("\\", "/")
        module = posixpath.normpath(module) if module else None
    if (
        not module
        or module == "."
        or module == ".."
        or module.startswith("../")
        or module.startswith("/")
        or re.match(r"^[A-Za-z]:/", module)
    ):
        return None
    return module


def _module_coordinate(
    *,
    tool: str,
    report_path: str,
    domain_id: str,
    module_outcomes: Sequence[Mapping[str, Any]],
    identity_module: str,
    gradle_project_map: Mapping[str, str],
) -> Optional[str]:
    if tool == "python":
        # pytest reports are intentionally centralized outside the checkout, so
        # their path says nothing about the source module.  The testcase's own
        # source/class module is stable across invocation cwd changes and is
        # mechanically scoped by the surveyed domain carried on the receipt.
        raw_module = str(identity_module or "").strip().replace("\\", "/")
        while raw_module.startswith("./"):
            raw_module = raw_module[2:]
        if raw_module.startswith("/"):
            return None
        normalized = posixpath.normpath(raw_module)
        if (
            not normalized
            or normalized == "."
            or normalized == ".."
            or normalized.startswith("../")
        ):
            return None
        return f"pytest:{normalized}"
    module_root = _report_module_root(tool, report_path)
    relative = _relative_root(module_root or "", domain_id)
    if relative is None:
        return None
    if tool == "maven":
        # Maven's standard target/{surefire,failsafe}-reports boundary proves
        # the module root.  A relative module root is an allowed coordinate in
        # module-qualified-v1 when a GAV was not sealed by the runner.
        return relative
    if tool == "gradle":
        coordinate = gradle_project_map.get(posixpath.normpath(module_root or ""))
        if not coordinate:
            return None
        observed = {
            (
                ":root"
                if str(item.get("module") or "").strip(":") == "root"
                else ":" + str(item.get("module") or "").strip(":")
            )
            for item in module_outcomes
            if isinstance(item, Mapping) and str(item.get("module") or "").strip()
        }
        # Both facts are required: settings.gradle proves projectDir -> project
        # path, and the runner task stream proves this invocation named it.
        return coordinate if coordinate in observed else None
    return None


def _owner(module_or_file: str, raw_classname: str, fallback_class: str) -> str:
    """Stable owner without dropping enclosing/nested class components."""

    classname = str(raw_classname or "").strip().replace("$", ".").strip(".")
    classname = classname or str(fallback_class or "").strip()
    if not classname:
        return ""
    if "/" in module_or_file or module_or_file.endswith((".py", ".java", ".kt", ".scala")):
        return "::".join(part for part in (module_or_file, classname) if part)
    return classname


def _execution_identity_material(row: Mapping[str, Any]) -> dict[str, Any]:
    material = {
        key: row.get(key)
        for key in (
            "run_id",
            "receipt_id",
            "execution_index",
            "execution_ordinal",
            "target_sha",
            "domain_id",
            "module_coordinate",
            "framework",
            "owner",
            "test_name",
            "parameter_id",
            "report_path",
            "report_sha256",
        )
    }
    return material


def testcase_execution_id(row: Mapping[str, Any]) -> str:
    """Canonical execution identity shared by writer, reader, and evaluator."""

    digest = hashlib.sha256(
        json.dumps(
            _execution_identity_material(row),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"{str(row.get('receipt_id') or '').strip()}#execution-{digest}"


def _required_text(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TestcaseRowContractError(f"testcase execution row requires {key}")
    return value.strip()


def validate_testcase_execution_row(
    raw: Mapping[str, Any],
    *,
    receipt_id: Optional[str] = None,
    run_id: Optional[str] = None,
    target_sha: Optional[str] = None,
    domain_id: Optional[str] = None,
    report_claims: Optional[set[tuple[str, str]]] = None,
) -> dict[str, Any]:
    """Validate and normalize one current row; legacy/partial rows fail closed."""

    if not isinstance(raw, Mapping):
        raise TestcaseRowContractError("testcase execution row must be an object")
    row: dict[str, Any] = {
        key: _required_text(raw, key)
        for key in (
            "execution_id",
            "run_id",
            "receipt_id",
            "target_sha",
            "domain_id",
            "module_coordinate",
            "framework",
            "owner",
            "test_name",
            "outcome",
            "report_path",
            "report_sha256",
            "disposition",
        )
    }
    if row["disposition"] != "claimed" or raw.get("qualifying_invocation") is not True:
        raise TestcaseRowContractError(
            "testcase execution row requires disposition='claimed' and "
            "qualifying_invocation=true"
        )
    if row["outcome"] not in _ROW_OUTCOMES:
        raise TestcaseRowContractError(f"unsupported testcase outcome {row['outcome']!r}")
    row["report_sha256"] = row["report_sha256"].lower()
    if not _HEX_SHA256_RE.fullmatch(row["report_sha256"]):
        raise TestcaseRowContractError("testcase execution report hash is invalid")
    for key in ("execution_index", "execution_ordinal"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise TestcaseRowContractError(f"testcase execution {key} must be positive")
        row[key] = value
    parameter = raw.get("parameter_id")
    if "parameter_id" not in raw or not isinstance(parameter, (str, type(None))):
        raise TestcaseRowContractError("testcase execution parameter_id must be a string or null")
    row["parameter_id"] = parameter
    row["qualifying_invocation"] = True

    expected = {
        "receipt_id": receipt_id,
        "run_id": run_id,
        "target_sha": target_sha,
        "domain_id": domain_id,
    }
    for key, value in expected.items():
        if value is not None and row[key] != str(value).strip():
            raise TestcaseRowContractError(f"testcase execution {key} does not match receipt")
    sequence = _receipt_sequence(row["receipt_id"])
    if sequence is None or row["execution_index"] != sequence:
        raise TestcaseRowContractError("testcase execution index is not receipt ordered")
    binding = (row["report_path"], row["report_sha256"])
    if report_claims is not None and binding not in report_claims:
        raise TestcaseRowContractError("testcase execution is not bound to its report delta")
    expected_execution_id = testcase_execution_id(row)
    if row["execution_id"] != expected_execution_id:
        raise TestcaseRowContractError("testcase execution_id does not match its identity material")
    return row


def _row_subject_identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(
        row.get(key)
        for key in (
            "run_id",
            "target_sha",
            "domain_id",
            "module_coordinate",
            "framework",
            "owner",
            "test_name",
        )
    )


def _count_row_outcomes(outcomes: Sequence[str]) -> dict[str, int]:
    counts = {"executed": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    fields = {"passed": "passed", "failed": "failed", "error": "errors", "skipped": "skipped"}
    for outcome in outcomes:
        if outcome not in fields:
            raise TestcaseRowContractError(f"unsupported testcase outcome {outcome!r}")
        counts["executed"] += 1
        counts[fields[outcome]] += 1
    return counts


def aggregate_testcase_execution_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One aggregation law for production metrics and the campaign evaluator."""

    executions: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = validate_testcase_execution_row(raw)
        execution_id = row["execution_id"]
        prior_execution = executions.get(execution_id)
        if prior_execution is not None and prior_execution != row:
            raise TestcaseRowContractError(f"conflicting duplicate execution_id {execution_id!r}")
        executions[execution_id] = row

    by_case: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in executions.values():
        by_case.setdefault((*_row_subject_identity(row), row["parameter_id"]), []).append(row)

    latest_cases: list[dict[str, Any]] = []
    retried_cases = 0
    flaky_cases = 0
    for case, history in by_case.items():
        by_invocation: dict[tuple[int, str], list[dict[str, Any]]] = {}
        index_receipts: dict[int, str] = {}
        for row in history:
            index = int(row["execution_index"])
            receipt = str(row["receipt_id"])
            previous_receipt = index_receipts.get(index)
            if previous_receipt is not None and previous_receipt != receipt:
                raise TestcaseRowContractError(
                    f"case {case!r} has multiple receipts at execution_index {index}"
                )
            index_receipts[index] = receipt
            by_invocation.setdefault((index, receipt), []).append(row)

        attempts: list[dict[str, Any]] = []
        for key in sorted(by_invocation):
            invocation_rows = by_invocation[key]
            worst = max(
                invocation_rows,
                key=lambda row: (_OUTCOME_SEVERITY[str(row["outcome"])], row["execution_id"]),
            )
            attempts.append(worst)
        latest = attempts[-1]
        latest_cases.append(latest)
        if len(attempts) > 1:
            retried_cases += 1
        if latest["outcome"] == "passed" and any(
            row["outcome"] in {"failed", "error"} for row in attempts[:-1]
        ):
            flaky_cases += 1

    subject_outcomes: dict[tuple[Any, ...], str] = {}
    for row in latest_cases:
        subject = _row_subject_identity(row)
        outcome = str(row["outcome"])
        prior_outcome = subject_outcomes.get(subject)
        if prior_outcome is None or _OUTCOME_SEVERITY[outcome] > _OUTCOME_SEVERITY[prior_outcome]:
            subject_outcomes[subject] = outcome

    return {
        "claimed": {
            "latest_subjects": _count_row_outcomes(list(subject_outcomes.values())),
            "latest_cases": _count_row_outcomes([str(row["outcome"]) for row in latest_cases]),
            "receipt_executions": _count_row_outcomes(
                [str(row["outcome"]) for row in executions.values()]
            ),
        },
        "retried_cases": retried_cases,
        "flaky_cases": flaky_cases,
    }


def seal_testcase_execution_rows(
    parsed: Mapping[str, Any] | None,
    *,
    run_id: Optional[str],
    receipt_id: str,
    tool: str,
    target_sha: Optional[str],
    domain_id: Optional[str],
    working_directory: str,
    module_outcomes: Sequence[Mapping[str, Any]] = (),
    gradle_project_map: Mapping[str, str] | None = None,
) -> Optional[dict[str, Any]]:
    """Bind parsed rows to one receipt, or return an unavailable envelope."""

    # Kept in the public call shape for receipt compatibility; cwd is an
    # execution coordinate and deliberately cannot enter subject identity.
    del working_directory
    if parsed is None:
        return None
    reasons = {str(reason) for reason in parsed.get("reasons") or () if str(reason).strip()}
    if parsed.get("status") != "complete":
        reasons.add("report_rows_incomplete")
    raw_rows = parsed.get("rows")
    if not isinstance(raw_rows, list):
        reasons.add("testcase_rows_invalid")
        raw_rows = []
    report_count = parsed.get("report_count")
    if isinstance(report_count, bool) or not isinstance(report_count, int) or report_count < 1:
        reasons.add("report_count_invalid")
        report_count = 0
    normalized_tool = str(tool or "").strip().lower()
    if normalized_tool not in {"maven", "gradle", "python"}:
        reasons.add("framework_unavailable")
    sha = str(target_sha or "").strip()
    domain = str(domain_id or "").strip()
    run = str(run_id or "").strip()
    sequence = _receipt_sequence(receipt_id)
    if not run:
        reasons.add("run_id_unavailable")
    if not sha:
        reasons.add("target_sha_unavailable")
    if not domain:
        reasons.add("domain_id_unavailable")
    if sequence is None:
        reasons.add("invocation_sequence_unavailable")

    sealed: dict[str, dict[str, Any]] = {}
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            reasons.add("testcase_row_invalid")
            continue
        identity = canonical_test_identity(
            str(raw.get("classname") or ""),
            str(raw.get("name") or ""),
            str(raw.get("source_file") or "") or None,
        )
        identity_module = identity.module_or_file if identity else ""
        if normalized_tool == "python" and identity is not None:
            identity_module = (
                _python_module_identity(
                    source_file=str(raw.get("source_file") or ""),
                    fallback_module=identity.module_or_file,
                    domain_id=domain,
                )
                or ""
            )
        report_path = str(raw.get("report_path") or "").strip()
        report_sha256 = str(raw.get("report_sha256") or "").strip().lower()
        coordinate = _module_coordinate(
            tool=normalized_tool,
            report_path=report_path,
            domain_id=domain,
            module_outcomes=module_outcomes,
            identity_module=identity_module,
            gradle_project_map=gradle_project_map or {},
        )
        owner = (
            _owner(
                identity_module,
                str(raw.get("classname") or ""),
                identity.class_name,
            )
            if identity
            else ""
        )
        try:
            outcome = normalize_test_status(str(raw.get("outcome") or ""))
        except ValueError:
            outcome = None
        if (
            identity is None
            or not owner
            or not coordinate
            or not report_path
            or not re.fullmatch(r"[0-9a-f]{64}", report_sha256)
            or outcome is None
            or not sha
            or not domain
            or not run
            or sequence is None
        ):
            reasons.add("module_qualified_identity_unavailable")
            continue
        row: dict[str, Any] = {
            "run_id": run,
            "receipt_id": receipt_id,
            "execution_index": sequence,
            "execution_ordinal": raw.get("execution_ordinal"),
            "target_sha": sha,
            "domain_id": domain,
            "module_coordinate": coordinate,
            "framework": "pytest" if normalized_tool == "python" else "junit-xml",
            "owner": owner,
            "test_name": identity.name,
            "parameter_id": identity.param_id or None,
            "outcome": outcome,
            "report_path": report_path,
            "report_sha256": report_sha256,
            "disposition": "claimed",
            "qualifying_invocation": True,
        }
        ordinal = row["execution_ordinal"]
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
            reasons.add("execution_ordinal_unavailable")
            continue
        row["execution_id"] = testcase_execution_id(row)
        existing = sealed.get(row["execution_id"])
        if existing is None:
            sealed[row["execution_id"]] = row
        elif existing != row:
            reasons.add("execution_identity_conflict")

    status = "complete" if not reasons else "unavailable"
    return {
        "schema_version": ROW_ENVELOPE_VERSION,
        "status": status,
        "report_count": report_count,
        # An unavailable envelope may retain parser diagnostics elsewhere on
        # the receipt, but never exposes a tempting partial claim set.
        "rows": [sealed[key] for key in sorted(sealed)] if status == "complete" else [],
        **({"reasons": sorted(reasons)} if reasons else {}),
    }


__all__ = [
    "DELTA_PER_FILE_ROW_CAP",
    "DELTA_REPORT_MAX_BYTES",
    "DELTA_ROW_MAX_JSON_BYTES",
    "DELTA_TESTCASE_ROW_CAP",
    "ROW_ENVELOPE_VERSION",
    "SEALED_ROW_OVERHEAD_MAX_BYTES",
    "TestcaseRowContractError",
    "aggregate_testcase_execution_rows",
    "bound_testcase_rows",
    "diagnostic_testcase_outcomes",
    "read_delta_testcase_rows",
    "read_gradle_project_map",
    "rows_were_bounded",
    "seal_testcase_execution_rows",
    "testcase_execution_id",
    "validate_testcase_execution_row",
]

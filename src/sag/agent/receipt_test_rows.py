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


class TestcaseRowContractError(ValueError):
    """A row cannot enter the module-qualified metrics-v2 claim set."""


# The input path is the sole argv value.  Report paths therefore never enter a
# shell command, and a reactor with thousands of reports cannot hit ARG_MAX.
_CONTAINER_REPORT_ROW_PARSER = r"""
import json
import sys
import xml.etree.ElementTree as ET
from hashlib import sha256


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
            value = " ".join((child.get("message") or "").split())
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


result = {"status": "complete", "report_count": 0, "rows": [], "reasons": []}
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        reports = json.load(handle)
except Exception as exc:
    print(json.dumps({"status": "unavailable", "rows": [], "reasons": ["input_unreadable"]}))
    raise SystemExit(0)

for report in reports:
    path = report.get("path")
    expected = str(report.get("sha256") or "").lower()
    try:
        with open(path, "rb") as handle:
            body = handle.read()
        if sha256(body).hexdigest() != expected:
            result["status"] = "unavailable"
            result["reasons"].append("report_hash_mismatch")
            continue
        root = ET.fromstring(body)
    except Exception:
        result["status"] = "unavailable"
        result["reasons"].append("report_unreadable")
        continue
    result["report_count"] += 1
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
    for testcase_ordinal, testcase in enumerate(runtime, 1):
        status = outcome(testcase)
        result["rows"].append(
            {
                "report_path": path,
                "report_sha256": expected,
                "classname": (testcase.get("classname") or "").strip(),
                "name": (testcase.get("name") or "").strip(),
                "source_file": testcase.get("file") or suite_files.get(id(testcase)),
                "outcome": status,
                "reason": reason(testcase, status),
                "execution_ordinal": testcase_ordinal,
            }
        )

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


def read_delta_testcase_rows(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    *,
    receipt_id: str,
    delta: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    """Read every runtime testcase from this receipt's hash-bound reports.

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
    command = (
        f"python3 -c {shlex.quote(_CONTAINER_REPORT_ROW_PARSER)} " f"{shlex.quote(input_path)}"
    )
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
        return {
            "schema_version": ROW_ENVELOPE_VERSION,
            "status": (
                "complete"
                if payload.get("status") == "complete" and report_count_valid
                else "unavailable"
            ),
            "report_count": report_count if report_count_valid else 0,
            "rows": rows,
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
        name = str(raw.get("name") or "").strip()
        classname = str(raw.get("classname") or "").strip()
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
    if seen > _DIAGNOSTIC_CAP or parsed.get("status") != "complete":
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
    "ROW_ENVELOPE_VERSION",
    "TestcaseRowContractError",
    "aggregate_testcase_execution_rows",
    "diagnostic_testcase_outcomes",
    "read_delta_testcase_rows",
    "read_gradle_project_map",
    "seal_testcase_execution_rows",
    "testcase_execution_id",
    "validate_testcase_execution_row",
]

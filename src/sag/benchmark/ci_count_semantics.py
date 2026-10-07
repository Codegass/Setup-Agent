"""Source-bound CI test counts; no CI score or testcase identity comparison.

All entries in native case lists count as occurrences, without deduplication.
Legacy executed fields never provide the assessed denominator.
"""

from collections import Counter
from pathlib import Path
import re
from urllib.parse import urlsplit


COUNTS = ("reported_count", "skipped_count", "assessed_count", "passed_count",
          "failed_count", "error_count", "failed_or_error_count")
PARSER = "jenkins-test-report-v1"
_MAPPING = {
    "reported_count": "testReport.totalCount, or passCount + failCount + skipCount",
    "skipped_count": "testReport.skipCount",
    "assessed_count": "reported_count - skipped_count",
    "passed_count": "reported_count - skipCount - failCount; cross-checked against passCount where present",
    "failed_or_error_count": "testReport.failCount (Jenkins does not distinguish failure from error)",
    "failed_count": "unavailable: Jenkins failCount combines failures and errors",
    "error_count": "unavailable: Jenkins failCount combines failures and errors",
}


def unavailable(reason, *, sources=None, selected_url=None, selected_cell=None):
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Unavailable CI count semantics require a reason")
    return {"schema_version": 1, "status": "unavailable", "reason": reason,
            **{key: None for key in COUNTS}, "legacy_executed_semantics": "not_used",
            "selected_url": selected_url, "selected_cell": selected_cell,
            "sources": sources or {}}


def _integer(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"Invalid nonnegative integer: {name}")
    return value


def _ref(ref):
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
        raise ValueError("Missing CI source reference")
    path = Path(ref["path"])
    if path.is_absolute() or ".." in path.parts or not ref["path"]:
        raise ValueError("CI source path must be relative to the explicit evidence root")
    if not re.fullmatch(r"[0-9a-f]{64}", str(ref.get("sha256", ""))):
        raise ValueError("Invalid CI source digest")
    _integer(ref.get("bytes"), "source bytes")
    return ref


def _load(base, ref):
    from .requirements import bound_file, load_json

    path = bound_file(base, _ref(ref))
    if path.stat().st_size != ref["bytes"]:
        raise ValueError("CI source byte length mismatch")
    return load_json(path)


def _same_bytes(left, right):
    return all(left.get(k) == right.get(k) for k in ("sha256", "bytes"))


def _cases(value):
    """Return native outcome occurrence counts only when suites carry cases."""
    suites = value.get("suites")
    if suites is None:
        return None
    if not isinstance(suites, list):
        raise ValueError("Malformed Jenkins suites")
    counts = Counter(passed=0, failed_or_error=0, skipped=0)
    complete = True
    for suite in suites:
        if not isinstance(suite, dict):
            raise ValueError("Malformed Jenkins suite")
        cases = suite.get("cases")
        if cases is None:
            complete = False
            continue
        if not isinstance(cases, list):
            raise ValueError("Malformed Jenkins cases")
        for case in cases:
            if not isinstance(case, dict):
                raise ValueError("Malformed Jenkins case")
            state = case.get("status")
            outcome = {"PASSED": "passed", "FIXED": "passed", "FAILED": "failed_or_error",
                       "REGRESSION": "failed_or_error", "SKIPPED": "skipped"}.get(state)
            if outcome is None:
                raise ValueError("Unknown Jenkins case status")
            if "skipped" in case and (type(case["skipped"]) is not bool or case["skipped"] != (outcome == "skipped")):
                raise ValueError("Jenkins case skipped flag disagrees with status")
            counts[outcome] += 1
    return counts, complete


def _native_counts(value):
    if not isinstance(value, dict):
        raise ValueError("Malformed Jenkins test result")
    failed = _integer(value.get("failCount"), "failCount")
    skipped = _integer(value.get("skipCount"), "skipCount")
    total = value.get("totalCount")
    passed = value.get("passCount")
    if total is None:
        total = _integer(passed, "passCount") + failed + skipped
    total = _integer(total, "totalCount")
    inferred_passed = total - failed - skipped
    if inferred_passed < 0 or passed is not None and _integer(passed, "passCount") != inferred_passed:
        raise ValueError("Jenkins native counts do not conserve reported total")
    return Counter(passed=inferred_passed, failed_or_error=failed, skipped=skipped)


def _parse_report(value):
    if not isinstance(value, dict) or value.get("_class") not in {
        "hudson.maven.reporters.SurefireAggregatedReport", "hudson.tasks.junit.TestResult",
        "hudson.tasks.junit.TestResultAction",
    }:
        raise ValueError("Unsupported native CI report format; Jenkins testReport required")
    native = _native_counts(value)
    observed, all_cases = Counter(passed=0, failed_or_error=0, skipped=0), True
    children = value.get("childReports")
    if children is not None:
        if not isinstance(children, list):
            raise ValueError("Malformed Jenkins childReports")
        summed = Counter(passed=0, failed_or_error=0, skipped=0)
        for child in children:
            if not isinstance(child, dict) or not isinstance(child.get("result"), dict):
                raise ValueError("Malformed Jenkins child report")
            result = child["result"]
            counts = _native_counts(result)
            summed.update(counts)
            cases = _cases(result)
            if cases is None:
                all_cases = False
            else:
                rows, complete = cases
                if complete and rows != counts or any(rows[k] > counts[k] for k in counts):
                    raise ValueError("Jenkins case outcome occurrences disagree with child counts")
                observed.update(rows)
                all_cases &= complete
        if summed != native:
            raise ValueError("Jenkins child totals disagree with aggregate counts")
    else:
        cases = _cases(value)
        if cases is None:
            all_cases = False
        else:
            observed, all_cases = cases
    if all_cases and observed != native or any(observed[k] > native[k] for k in native):
        raise ValueError("Jenkins case outcome occurrences disagree with native counts")
    total = sum(native.values())
    return {"reported_count": total, "skipped_count": native["skipped"],
            "assessed_count": total - native["skipped"], "passed_count": native["passed"],
            "failed_or_error_count": native["failed_or_error"], "failed_count": None, "error_count": None,
            "case_count_reconciliation": {"status": "complete" if all_cases else "unavailable",
                                          "observed_occurrences": sum(observed.values()),
                                          "identity_comparison": "not_performed"}}


def _selected_source(index, selected_url, selected_cell):
    if index.get("selected_url") != selected_url or not selected_url:
        raise ValueError("CI index does not bind the selected build URL")
    if selected_cell is not None and index.get("selected_cell") != selected_cell:
        raise ValueError("CI index does not bind the selected cell")
    selected = urlsplit(selected_url)
    if selected.scheme not in {"https", "http"} or not selected.netloc:
        raise ValueError("Invalid selected CI URL")
    matches = []
    for ref in index.get("evidence", []):
        if not isinstance(ref, dict):
            raise ValueError("Malformed CI index evidence")
        url = urlsplit(ref.get("official_url") or "")
        if (url.scheme, url.netloc, url.path) == (
            selected.scheme, selected.netloc, selected.path.rstrip("/") + "/testReport/api/json"
        ):
            matches.append(ref)
    if len(matches) != 1:
        raise ValueError("Selected CI index must identify exactly one native Jenkins testReport")
    return _ref(matches[0])


def build_jenkins_count_semantics(index_ref, *, base, selected_url, selected_cell=None,
                                  report_ref=None, legacy_target_ref=None):
    """Import already archived bytes; unsupported or contradictory sources stay unknown.

    report_ref may point to a byte-identical relocated copy. The immutable index
    remains an independent anchor even when its historical path is unavailable.
    """
    sources = {"ci_index": index_ref}
    try:
        index = _load(base, index_ref)
        if not isinstance(index, dict):
            raise ValueError("Malformed frozen CI index")
        original = _selected_source(index, selected_url, selected_cell)
        report_ref = original if report_ref is None else report_ref
        if not _same_bytes(original, _ref(report_ref)):
            raise ValueError("Raw testReport differs from independent frozen index")
        sources["test_report"] = report_ref
        counts = _parse_report(_load(base, report_ref))
        indexed_counts = index.get("counts")
        if isinstance(indexed_counts, dict):
            for old, new in (("reported", "reported_count"), ("passed", "passed_count"),
                             ("failed_or_error", "failed_or_error_count"), ("skipped", "skipped_count")):
                if indexed_counts.get(old) is not None and _integer(indexed_counts[old], old) != counts[new]:
                    raise ValueError("Frozen index summary conflicts with native testReport counts")
        legacy = "not_used"
        if legacy_target_ref is not None:
            sources["legacy_target"] = legacy_target_ref
            target = _load(base, legacy_target_ref)
            if not isinstance(target, dict):
                raise ValueError("Malformed legacy CI target")
            note = "Counts include skipped final testcase rows"
            if not any(note in n for n in index.get("notes", []) if isinstance(n, str)):
                raise ValueError("Frozen index does not declare legacy executed semantics")
            if target.get("matched_cell") != index.get("selected_cell"):
                raise ValueError("Legacy target selected cell differs")
            cells = [c for c in target.get("cells", []) if isinstance(c, dict) and c.get("cell_id") == target["matched_cell"]]
            if len(cells) != 1 or any(_integer(cells[0].get(k), k) != counts[v] for k, v in (
                ("executed_count", "reported_count"), ("skipped", "skipped_count"),
                ("red_count", "failed_or_error_count"),
            )):
                raise ValueError("Legacy target does not match declared reported-count mapping")
            legacy = "reported_including_skipped"
        result = {"schema_version": 1, "status": "available", "producer": PARSER,
                  **counts, "failure_count_semantics": "combined", "field_mapping": dict(_MAPPING),
                  "legacy_executed_semantics": legacy, "selected_url": selected_url,
                  "selected_cell": index.get("selected_cell"), "sources": sources}
        return validate_count_semantics(result)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return unavailable(str(exc), sources=sources, selected_url=selected_url, selected_cell=selected_cell)


def validate_count_semantics(value, *, base=None):
    """Validate metadata; with an explicit root also recheck source bytes/counts."""
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("Missing or unsupported CI test count semantics")
    if value.get("status") == "unavailable":
        if not isinstance(value.get("reason"), str) or not value["reason"].strip():
            raise ValueError("Unknown CI test count semantics require a reason")
        if any(value.get(k) is not None for k in COUNTS):
            raise ValueError("Unavailable CI test counts must not supply denominators")
        return value
    if value.get("status") != "available" or value.get("producer") != PARSER:
        raise ValueError("Unsupported available CI count producer")
    for k in ("reported_count", "skipped_count", "assessed_count", "passed_count"):
        _integer(value.get(k), k)
    if value["reported_count"] != value["skipped_count"] + value["assessed_count"]:
        raise ValueError("reported_count must equal skipped_count + assessed_count")
    # This bounded producer exposes only Jenkins' combined failCount. A future
    # separate failure/error producer needs its own native source mapping.
    if value.get("failure_count_semantics") != "combined" or any(value.get(k) is not None for k in ("failed_count", "error_count")):
        raise ValueError("Jenkins failCount cannot be split into failed/error counts")
    failed = _integer(value.get("failed_or_error_count"), "failed_or_error_count")
    if value["assessed_count"] != value["passed_count"] + failed:
        raise ValueError("Assessed outcomes do not conserve native counts")
    if value.get("field_mapping") != _MAPPING:
        raise ValueError("Native count field mapping changed")
    reconciliation = value.get("case_count_reconciliation")
    if not isinstance(reconciliation, dict) or reconciliation.get("status") not in {"complete", "unavailable"} or reconciliation.get("identity_comparison") != "not_performed":
        raise ValueError("Invalid native case-count reconciliation status")
    observed = _integer(reconciliation.get("observed_occurrences"), "observed_occurrences")
    if observed > value["reported_count"] or reconciliation["status"] == "complete" and observed != value["reported_count"]:
        raise ValueError("Case outcome occurrence totals disagree")
    if value.get("legacy_executed_semantics") not in {"not_used", "reported_including_skipped"}:
        raise ValueError("Unsupported legacy executed semantics")
    sources = value.get("sources")
    if not isinstance(sources, dict) or not {"ci_index", "test_report"} <= set(sources):
        raise ValueError("CI count semantics require raw report and independent index")
    for source in sources.values():
        _ref(source)
    if value["legacy_executed_semantics"] == "reported_including_skipped" and "legacy_target" not in sources:
        raise ValueError("Legacy mapping requires its target bytes")
    if not isinstance(value.get("selected_url"), str) or not value["selected_url"]:
        raise ValueError("CI count semantics require selected build URL")
    if base is not None:
        actual = build_jenkins_count_semantics(sources["ci_index"], base=base,
            selected_url=value["selected_url"], selected_cell=value.get("selected_cell"),
            report_ref=sources["test_report"], legacy_target_ref=sources.get("legacy_target"))
        if actual != value:
            raise ValueError("CI count metadata differs from bound native source: " + actual.get("reason", "values changed"))
    return value

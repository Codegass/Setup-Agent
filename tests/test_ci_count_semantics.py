"""Native CI denominator semantics, from archived bytes without CI requests."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

import pytest

from sag.benchmark.ci_count_semantics import (
    build_jenkins_count_semantics, unavailable, validate_count_semantics,
)


def ref(base, path):
    raw = (base / path).read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


@pytest.fixture
def archive(tmp_path):
    with ZipFile(Path(__file__).parent / "fixtures/ci_count_semantics_jenkins.zip") as z:
        z.extractall(tmp_path)
    def load(name="commons-net", *, legacy=True):
        path = Path("ci") / name / "index.json"
        index = json.loads((tmp_path / path).read_text())
        kwargs = {"base": tmp_path, "selected_url": index["selected_url"],
                  "selected_cell": index["selected_cell"]}
        if legacy:
            kwargs["legacy_target_ref"] = ref(tmp_path, Path("historical-targets") / (name + ".json"))
        return ref(tmp_path, path), kwargs, index
    return load


@pytest.mark.parametrize("name,reported,skipped,assessed", [
    ("commons-net", 557, 2, 555), ("commons-dbutils", 523, 0, 523),
])
def test_real_archived_counts_separate_reported_and_assessed(archive, name, reported, skipped, assessed):
    index_ref, kwargs, _ = archive(name)
    result = build_jenkins_count_semantics(index_ref, **kwargs)
    assert result["status"] == "available", result
    assert [result[k] for k in ("reported_count", "skipped_count", "assessed_count")] == [reported, skipped, assessed]
    assert result["passed_count"] == assessed
    assert result["failed_or_error_count"] == 0
    assert result["failed_count"] is None and result["error_count"] is None
    assert result["legacy_executed_semantics"] == "reported_including_skipped"
    assert result["case_count_reconciliation"] == {
        "status": "complete", "observed_occurrences": reported, "identity_comparison": "not_performed",
    }
    assert validate_count_semantics(result, base=kwargs["base"]) == result


def test_new_denominator_never_depends_on_legacy_executed_field(archive):
    index_ref, kwargs, _ = archive(legacy=False)
    result = build_jenkins_count_semantics(index_ref, **kwargs)
    assert result["assessed_count"] == 555
    assert result["legacy_executed_semantics"] == "not_used"
    assert "legacy_target" not in result["sources"]


def mutate_report(archive, mutate):
    index_ref, kwargs, index = archive(legacy=False)
    base = kwargs["base"]
    original = next(r for r in index["evidence"] if "/testReport/api/json" in (r.get("official_url") or ""))
    path = base / original["path"]
    report = json.loads(path.read_text())
    mutate(report)
    path.write_text(json.dumps(report))
    original.update(ref(base, original["path"]))
    index_path = base / index_ref["path"]
    index_path.write_text(json.dumps(index))
    return ref(base, index_ref["path"]), kwargs


@pytest.mark.parametrize("change", [
    "missing_skip", "negative", "boolean", "wrong_total", "child_conflict",
    "omitted_case", "duplicated_case", "unknown_status", "flag_conflict", "unsupported_format",
])
def test_native_conflicts_do_not_become_available_denominators(archive, change):
    def mutate(report):
        child = report["childReports"][0]["result"]
        cases = child["suites"][0]["cases"]
        if change == "missing_skip": report.pop("skipCount")
        elif change == "negative": report["skipCount"] = -1
        elif change == "boolean": report["skipCount"] = True
        elif change == "wrong_total": report["totalCount"] -= 1
        elif change == "child_conflict": child["passCount"] += 1
        elif change == "omitted_case": cases.pop()
        elif change == "duplicated_case": cases.append(deepcopy(cases[0]))
        elif change == "unknown_status": cases[0]["status"] = "MAYBE_PASSED"
        elif change == "flag_conflict": cases[0]["skipped"] = True
        else: report["_class"] = "github.check_run"
    index_ref, kwargs = mutate_report(archive, mutate)
    result = build_jenkins_count_semantics(index_ref, **kwargs)
    assert result["status"] == "unavailable"
    assert result["reason"] and result["assessed_count"] is None
    validate_count_semantics(result)


def test_native_totals_without_case_rows_remain_counts_only(archive):
    index_ref, kwargs = mutate_report(archive, lambda r: r["childReports"][0]["result"].pop("suites"))
    result = build_jenkins_count_semantics(index_ref, **kwargs)
    assert result["status"] == "available"
    assert result["assessed_count"] == 555
    assert result["case_count_reconciliation"]["status"] == "unavailable"
    assert result["case_count_reconciliation"]["identity_comparison"] == "not_performed"


@pytest.mark.parametrize("status", ["FAILED", "REGRESSION"])
def test_native_combined_failure_is_retained_without_inventing_error_partition(archive, status):
    def mutate(report):
        report["failCount"] = 1
        result = report["childReports"][0]["result"]
        result["failCount"] = 1
        result["passCount"] -= 1
        result["suites"][0]["cases"][0]["status"] = status
    index_ref, kwargs = mutate_report(archive, mutate)
    base = kwargs["base"]
    index = json.loads((base / index_ref["path"]).read_text())
    index["counts"].update(passed=554, failed_or_error=1)
    (base / index_ref["path"]).write_text(json.dumps(index))
    value = build_jenkins_count_semantics(ref(base, index_ref["path"]), **kwargs)
    assert value["status"] == "available", value
    assert value["passed_count"] == 554 and value["failed_or_error_count"] == 1
    assert value["failed_count"] is value["error_count"] is None


@pytest.mark.parametrize("change", ["hash", "bytes", "selected_url", "selected_cell", "duplicate_source", "index_count"])
def test_index_and_raw_bytes_are_independent_anchors(archive, change):
    index_ref, kwargs, index = archive()
    base = kwargs["base"]
    original = next(r for r in index["evidence"] if "/testReport/api/json" in (r.get("official_url") or ""))
    if change == "hash": (base / original["path"]).write_text("{}")
    elif change == "bytes": index_ref["bytes"] += 1
    elif change == "selected_url": kwargs["selected_url"] += "another/"
    elif change == "selected_cell": kwargs["selected_cell"] = "another"
    else:
        if change == "duplicate_source": index["evidence"].append(deepcopy(original))
        else: index["counts"]["reported"] += 1
        (base / index_ref["path"]).write_text(json.dumps(index))
        index_ref = ref(base, index_ref["path"])
    result = build_jenkins_count_semantics(index_ref, **kwargs)
    assert result["status"] == "unavailable"


def test_relocated_source_bytes_keep_original_index_anchor(archive):
    index_ref, kwargs, index = archive(legacy=False)
    base = kwargs["base"]
    original = next(r for r in index["evidence"] if "/testReport/api/json" in (r.get("official_url") or ""))
    (base / "sources").mkdir()
    (base / "sources/report.json").write_bytes((base / original["path"]).read_bytes())
    (base / original["path"]).unlink()
    result = build_jenkins_count_semantics(index_ref, report_ref=ref(base, "sources/report.json"), **kwargs)
    assert result["status"] == "available", result
    validate_count_semantics(result, base=base)


@pytest.mark.parametrize("change", ["assessed", "split", "mapping", "source_missing", "legacy_guess", "case_claim", "native_changed"])
def test_validator_rejects_mutated_or_falsely_split_semantics(archive, change):
    index_ref, kwargs, _ = archive()
    result = build_jenkins_count_semantics(index_ref, **kwargs)
    if change == "assessed": result["assessed_count"] = result["reported_count"]
    elif change == "split": result.update(failed_count=0, error_count=0)
    elif change == "mapping": result["field_mapping"]["assessed_count"] = "legacy.executed_count"
    elif change == "source_missing": result["sources"].pop("test_report")
    elif change == "legacy_guess": result["legacy_executed_semantics"] = "assessed_excluding_skipped"
    elif change == "case_claim": result["case_count_reconciliation"]["identity_comparison"] = "equivalent"
    else: (kwargs["base"] / result["sources"]["test_report"]["path"]).write_text("{}")
    with pytest.raises(ValueError):
        validate_count_semantics(result, base=kwargs["base"])


def test_unavailable_needs_reason_and_cannot_supply_denominator():
    value = unavailable("GitHub artifacts are not covered by this importer")
    assert validate_count_semantics(value) == value
    value["assessed_count"] = 0
    with pytest.raises(ValueError): validate_count_semantics(value)
    with pytest.raises(ValueError): unavailable("")


def test_helper_imports_with_stdlib_only():
    root = Path(__file__).parents[1] / "src"
    result = subprocess.run([sys.executable, "-S", "-c",
        "import sys;sys.path.insert(0,sys.argv[1]);from sag.benchmark.ci_count_semantics import unavailable;print(unavailable('missing')['status'])",
        str(root)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "unavailable"

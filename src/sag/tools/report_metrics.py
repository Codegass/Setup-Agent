"""Assemble the structured build/test metrics artifact (report_metrics.json).

The report tool already computes everything in its in-memory snapshot; this
turns that into the on-disk contract the web read model consumes so the UI
never re-parses the markdown report. Missing values become null / []."""

from typing import Any, Dict, List, Optional

METRICS_PATH = "/workspace/.setup_agent/report_metrics.json"
METRICS_VERSION = 1
_MAX_FAILING = 50
_MAX_SAMPLES = 10


def _int_or_none(value: Any) -> Optional[int]:
    return value if isinstance(value, int) else None


def _float_or_none(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def _str_list(value: Any, limit: int) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value[:limit]]


def assemble_report_metrics(
    *,
    snapshot: Dict[str, Any],
    build_evidence: Dict[str, Any],
    test_analysis: Dict[str, Any],
    conflicts: List[str],
    evidence_refs: List[str],
    generated_at: str,
    execution_metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    status = (snapshot or {}).get("status") or {}
    evidence = (snapshot or {}).get("physical_evidence") or {}
    build_evidence = build_evidence or {}
    test_analysis = test_analysis or {}
    execution_metrics = execution_metrics or {}

    build = {
        "state": status.get("overall") if isinstance(status.get("overall"), str) else None,
        "system": build_evidence.get("build_system") or build_evidence.get("system"),
        "tool": build_evidence.get("tool"),
        "class_count": _int_or_none(evidence.get("class_files")),
        "jar_count": _int_or_none(evidence.get("jar_files")),
        "module_output_count": _int_or_none(build_evidence.get("module_output_count")),
        "artifact_samples": _str_list(build_evidence.get("artifact_samples"), _MAX_SAMPLES),
        "warnings": _str_list(build_evidence.get("warnings"), 25),
        "evidence_refs": _str_list(evidence_refs, 25),
        "time": build_evidence.get("build_time"),
        "note": build_evidence.get("build_command"),
        "artifact": build_evidence.get("artifact")
            or (_str_list(build_evidence.get("artifact_samples"), 1) or [None])[0],
    }

    test = {
        "state": status.get("overall") if isinstance(status.get("overall"), str) else None,
        "total": _int_or_none(status.get("tests_total")),
        "passed": _int_or_none(status.get("tests_passed")),
        "failed": _int_or_none(status.get("tests_failed")),
        "errors": _int_or_none(status.get("tests_errors")),
        "skipped": _int_or_none(status.get("tests_skipped")),
        "pass_rate": _float_or_none(status.get("pass_pct")),
        "report_file_count": _int_or_none(test_analysis.get("report_file_count")),
        "unique_total": _int_or_none(status.get("tests_unique")),
        "unique_passed": _int_or_none(status.get("tests_passed_unique")),
        "unique_failed": _int_or_none(status.get("tests_failed_unique")),
        "unique_errors": _int_or_none(status.get("tests_errors_unique")),
        "unique_skipped": _int_or_none(status.get("tests_skipped_unique")),
        "declared_total": _int_or_none(status.get("static_test_count")),
        "method_execution_rate": _float_or_none(status.get("execution_rate")),
        "failing_names": _str_list(test_analysis.get("failing_test_names"), _MAX_FAILING),
        "conflicts": _str_list(conflicts, 25),
        "evidence_refs": _str_list(evidence_refs, 25),
    }

    model = execution_metrics.get("model")
    return {
        "version": METRICS_VERSION,
        "generated_at": generated_at,
        # Runtime metadata for the web read model's DetailHeader chips. The web
        # read model (_setup_artifact_item) reads these top-level keys directly.
        "model": str(model) if isinstance(model, str) and model else None,
        "total_iterations": _int_or_none(execution_metrics.get("total_iterations")),
        "max_iterations": _int_or_none(execution_metrics.get("max_iterations")),
        "build": build,
        "test": test,
    }

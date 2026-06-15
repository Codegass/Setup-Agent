"""Assemble the per-submodule build/test metrics artifact (module_metrics.json).

Pure reconciliation of three inputs the report tool gathers:
- physical per-module scan (the backbone: which modules exist + artifacts + report dirs),
- the build tool's reactor status (already persisted in test_summary.jsonl for Maven),
- per-module test counts parsed from each module's report XML.

Mirrors report_metrics.py: a single pure function, missing values -> null/[].
"""

from typing import Any, Dict, List, Optional

MODULE_METRICS_PATH = "/workspace/.setup_agent/module_metrics.json"
MODULE_METRICS_VERSION = 1
_MAX_FAILING = 500
_MAX_ERROR_SAMPLES = 20

_BUILD_STATES = {"success", "failure", "skipped", "unknown"}


def _int_or_none(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_list(value: Any, limit: int) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value[:limit]]


def _norm_status(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    low = value.strip().lower()
    return low if low in _BUILD_STATES else None


def assemble_module_metrics(
    *,
    modules: List[Dict[str, Any]],
    reactor_status: Dict[str, str],
    tests: Dict[str, Dict[str, Any]],
    build_systems: List[str],
    build_error_samples: Dict[str, List[str]],
    generated_at: str,
) -> Dict[str, Any]:
    reactor_status = reactor_status or {}
    tests = tests or {}
    build_error_samples = build_error_samples or {}
    out_modules: List[Dict[str, Any]] = []

    any_failure = any(_norm_status(v) == "failure" for v in reactor_status.values())

    for scan in modules or []:
        path = str(scan.get("path") or "")
        name = str(scan.get("name") or path or ".")
        class_count = _int_or_none(scan.get("class_count"))
        jar_count = _int_or_none(scan.get("jar_count"))

        # Build status: reactor (by name) wins; else infer from artifacts.
        reactor = _norm_status(reactor_status.get(name)) or _norm_status(reactor_status.get(path))
        if reactor is not None:
            build_status, build_source = reactor, "reactor"
            # Conflict guard: reactor says success but nothing was produced.
            if reactor == "success" and not (class_count or jar_count) and path != ".":
                build_source = "partial"
        elif class_count or jar_count:
            build_status, build_source = "success", "artifacts"
        elif any_failure:
            build_status, build_source = "skipped", "partial"
        else:
            build_status, build_source = "unknown", "none"

        t = tests.get(path) or {}
        has_tests = bool(t)
        failing_names = _str_list(t.get("failing_names"), _MAX_FAILING)
        failing_count = _int_or_none(t.get("failing_count"))
        if failing_count is None and has_tests:
            failing_count = len(t.get("failing_names") or [])

        out_modules.append({
            "name": name,
            "path": path,
            "build_status": build_status,
            "build_source": build_source,
            "class_count": class_count,
            "jar_count": jar_count,
            "build_warnings": _int_or_none(scan.get("build_warnings")),
            "build_error_samples": _str_list(build_error_samples.get(path), _MAX_ERROR_SAMPLES),
            "tests_total": _int_or_none(t.get("tests_total")),
            "tests_passed": _int_or_none(t.get("tests_passed")),
            "tests_failed": _int_or_none(t.get("tests_failed")),
            "tests_errors": _int_or_none(t.get("tests_errors")),
            "tests_skipped": _int_or_none(t.get("tests_skipped")),
            "test_source": "runner_xml" if has_tests else "none",
            "failing_names": failing_names,
            "failing_count": failing_count,
            "evidence_refs": _str_list(t.get("evidence_refs") or scan.get("report_dirs"), 25),
        })

    total = len(out_modules)
    summary = {
        "modules_total": total,
        "modules_built": sum(1 for m in out_modules if m["build_status"] == "success"),
        "modules_failed": sum(1 for m in out_modules if m["build_status"] == "failure"),
        "modules_skipped": sum(1 for m in out_modules if m["build_status"] == "skipped"),
        "modules_with_test_failures": sum(
            1 for m in out_modules if (m["failing_count"] or 0) > 0
        ),
        "build_systems": _str_list(build_systems, 5),
        "single_module": total <= 1,
    }
    return {
        "version": MODULE_METRICS_VERSION,
        "generated_at": generated_at,
        "module_summary": summary,
        "modules": out_modules,
    }

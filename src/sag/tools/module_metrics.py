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


def _norm_key(value: Any) -> str:
    """Collapse a module name / path / reactor label to a comparable key.

    Maven reactor labels use the descriptive <name> (e.g.
    "Apache Kafka :: Connect :: API") while scan_modules derives keys from the
    directory path ("connect:api" / "connect/api"). Lowercasing and replacing
    every '::', ':' and '/' separator with a single space lets the two line up.
    """
    if not isinstance(value, str):
        return ""
    text = value.lower()
    for sep in ("::", ":", "/"):
        text = text.replace(sep, " ")
    return " ".join(text.split())


def _build_reactor_index(reactor_status: Dict[str, str]) -> Dict[str, str]:
    """Normalized index of reactor keys -> status (last write wins)."""
    index: Dict[str, str] = {}
    for label, status in reactor_status.items():
        norm = _norm_key(label)
        if not norm:
            continue
        index[norm] = status
        # Also index the trailing segment (e.g. "...:: Connect :: API" -> "api")
        # so a descriptive label still matches a single-segment module name.
        tail = norm.rsplit(" ", 1)[-1]
        index.setdefault(tail, status)
    return index


def _match_reactor_key(index: Dict[str, str], name: str, path: str) -> Optional[str]:
    """Return the reactor index KEY a scanned module matches, or None.

    Same resolution order as :func:`_lookup_reactor` (full normalized name/path,
    then trailing path segment) but returns the key so the caller can dedupe and
    detect which reactor entries a disk scan covered.
    """
    for candidate in (_norm_key(name), _norm_key(path)):
        if candidate and candidate in index:
            return candidate
    tail = _norm_key(path).rsplit(" ", 1)[-1]
    if tail and tail in index:
        return tail
    return None


def _lookup_reactor(
    index: Dict[str, str], name: str, path: str
) -> Optional[str]:
    """Find a reactor status for a scanned module by normalized name/path.

    Tries the full normalized name and path first, then the trailing path
    segment (e.g. "connect/api" -> "api") so descriptive Maven <name> labels
    that were indexed by tail still resolve.
    """
    key = _match_reactor_key(index, name, path)
    return index.get(key) if key else None


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
    reactor_index = _build_reactor_index(reactor_status)
    # When a live Maven Reactor Summary was captured it is AUTHORITATIVE for the
    # "detected" module set: the detected modules are exactly the modules Maven
    # built. A scanned dir that is not in the reactor is not part of the build
    # (e.g. a standalone example pom) and is dropped; reactor entries that no disk
    # scan matched still get a row. Without a reactor summary the caller has
    # already narrowed `modules` to the active reactor-declared set.
    reactor_present = bool(reactor_index)
    matched_reactor_keys: set[str] = set()

    for scan in modules or []:
        path = str(scan.get("path") or "")
        name = str(scan.get("name") or path or ".")
        class_count = _int_or_none(scan.get("class_count"))
        jar_count = _int_or_none(scan.get("jar_count"))

        # Mixed maven+gradle layout (live bigtop): the reactor summary is a
        # MAVEN artifact, so it speaks only for maven-scanned rows. Rows found
        # EXCLUSIVELY by the gradle scan must neither be dropped by the
        # authoritative-reactor filter (they can never appear in a maven
        # reactor) nor inherit reactor-derived inference. A collision row —
        # found by BOTH scans (dual-marker dir, or the root "." that every
        # mixed scan contains) — keeps its maven membership even when the
        # gradle record won on richness: it still gets the reactor match, so
        # the reactor verdict wins and its reactor entry is consumed (no
        # phantom fallback row). Untagged rows (single-system scans) keep the
        # exact pre-merge behavior.
        scan_system = str(scan.get("scan_build_system") or "").strip().lower()
        gradle_scanned = bool(scan_system) and scan_system != "maven"
        both_scans = bool(scan.get("scan_found_by_both"))
        reactor_exempt = gradle_scanned and not both_scans

        # Build status: reactor wins; match descriptive Maven <name> labels by
        # normalizing both sides (name, path, trailing path segment).
        reactor_key = None if reactor_exempt else _match_reactor_key(reactor_index, name, path)
        reactor = _norm_status(reactor_index.get(reactor_key)) if reactor_key else None

        if reactor_present and not reactor_exempt:
            # Authoritative reactor: skip scanned dirs not in the reactor, and
            # dedupe if two scanned dirs map to the same reactor entry.
            if reactor_key is not None and reactor_key not in matched_reactor_keys:
                matched_reactor_keys.add(reactor_key)
            elif gradle_scanned:
                # Gradle-won collision row outside the captured reactor (or a
                # duplicate tail-match): maven does not speak for it, but the
                # gradle evidence does — keep it as a gradle row rather than
                # dropping the gradle cluster.
                reactor = None
            else:
                continue

        if reactor is not None:
            build_status, build_source = reactor, "reactor"
            # Conflict guard: reactor says success but nothing was produced.
            if reactor == "success" and not (class_count or jar_count) and path != ".":
                build_source = "partial"
        elif (class_count or 0) > 0:
            # No reactor summary: infer "built" from FRESH compiled classes only.
            # ponytail: a jar with no .class files is NOT counted as built — it's
            # usually a stale jar left from a prior run while this build's modules
            # failed dependency resolution (commons-vfs read 7/7 with 4 dep
            # failures). Such a module stays detected but not built.
            build_status, build_source = "success", "artifacts"
        elif any_failure and not gradle_scanned:
            # A maven reactor failure implies downstream MAVEN modules were
            # skipped; it says nothing about a coexisting gradle cluster (nor
            # about a gradle-won collision row the reactor did not match).
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
            "has_test_sources": bool(scan.get("has_test_sources")),
            "failing_names": failing_names,
            "failing_count": failing_count,
            "evidence_refs": _str_list(t.get("evidence_refs") or scan.get("report_dirs"), 25),
        })

    # Reactor entries that no disk scan matched were still built by Maven — count
    # them (one row each) so "detected" equals the reactor module count exactly.
    # A scanned module may have matched via the full normalized key OR the trailing
    # segment (see _match_reactor_key), so skip a label if EITHER resolves to an
    # already-counted module — otherwise a tail-matched module is double-counted.
    if reactor_present:
        for label, status in reactor_status.items():
            full_key = _norm_key(label)
            tail_key = full_key.rsplit(" ", 1)[-1] if full_key else ""
            if not full_key:
                continue
            if full_key in matched_reactor_keys or (tail_key and tail_key in matched_reactor_keys):
                continue
            matched_reactor_keys.add(full_key)
            out_modules.append({
                "name": label,
                "path": "",
                "build_status": _norm_status(status) or "unknown",
                "build_source": "reactor",
                "class_count": None,
                "jar_count": None,
                "build_warnings": None,
                "build_error_samples": [],
                "tests_total": None,
                "tests_passed": None,
                "tests_failed": None,
                "tests_errors": None,
                "tests_skipped": None,
                "test_source": "none",
                # No disk scan matched this reactor entry, so test sources are
                # unknowable — default False so it can never inflate the
                # test-bearing count (and thus never fire the scope conflict).
                "has_test_sources": False,
                "failing_names": [],
                "failing_count": None,
                "evidence_refs": [],
            })

    total = len(out_modules)
    tested = sum(1 for m in out_modules if (m["tests_total"] or 0) > 0)
    summary = {
        "modules_total": total,
        "modules_built": sum(1 for m in out_modules if m["build_status"] == "success"),
        "modules_failed": sum(1 for m in out_modules if m["build_status"] == "failure"),
        "modules_skipped": sum(1 for m in out_modules if m["build_status"] == "skipped"),
        "modules_tested": tested,
        "modules_not_tested": total - tested,
        "modules_test_bearing": sum(
            1 for m in out_modules if m.get("has_test_sources")
        ),
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

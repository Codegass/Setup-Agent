"""Server-composed one-sentence verdict for the Workbench detail band."""

from __future__ import annotations


def _tone(outcome: str, build: dict | None, test: dict | None) -> str:
    o = (outcome or "").lower()
    if "fail" in o or (build and str(build.get("state", "")).lower() in {"failed", "failure"}):
        return "failed"
    if "partial" in o or (
        test and int(test.get("fail", 0) or 0) + int(test.get("errors", 0) or 0) > 0
    ):
        return "attention"
    if "success" in o or "pass" in o:
        return "success"
    return "attention"


def _canonical_tone(verdict: str) -> str:
    return {
        "success": "success",
        "partial": "attention",
        "failed": "failed",
        "unknown": "attention",
    }.get(verdict, "attention")


def _build_clause(build: dict | None, ms: dict | None) -> str | None:
    if not build:
        return None
    state = str(build.get("state", "")).lower()
    if ms and not ms.get("singleModule", False) and ms.get("modulesTotal"):
        total, built = int(ms["modulesTotal"]), int(ms.get("modulesBuilt", 0) or 0)
        if built >= total:
            return f"Build passed on all {total} modules"
        return (
            f"Build passed on {built} of {total} modules"
            if built
            else f"Build failed; 0 of {total} modules compiled"
        )
    if state in {"failed", "failure"}:
        return "Build failed"
    if state in {"success", "ok"}:
        return "Build passed"
    if state == "partial":
        return "Build partially completed"
    if state in {"unknown", "unavailable"}:
        return "Build result unavailable"
    return None


def _test_clause(test: dict | None) -> str | None:
    if not test:
        return None
    total = int(test.get("total", 0) or 0)
    fail = int(test.get("fail", 0) or 0) + int(test.get("errors", 0) or 0)
    state = str(test.get("state", "")).lower()
    if total <= 0:
        return "Test result unavailable" if state in {"unknown", "unavailable"} else None
    if fail == 0:
        return f"Test run passed with {total:,} sealed results"
    return f"Test run recorded {fail:,} non-passing results of {total:,}"


def compose_verdict(
    *,
    build,
    test,
    module_summary,
    outcome,
    blocker,
    canonical_verdict: str | None = None,
    verdict_source: str = "derived",
) -> dict | None:
    # Module rollups are mutable report diagnostics. A canonical snapshot view
    # may use its literal build/test evidence, but never module-derived wording.
    authoritative_modules = None if verdict_source == "snapshot" else module_summary
    clauses = [c for c in (_build_clause(build, authoritative_modules), _test_clause(test)) if c]
    if not clauses and canonical_verdict is None:
        return None
    if canonical_verdict is not None and verdict_source != "legacy":
        tone = _canonical_tone(canonical_verdict)
    else:
        tone = _tone(outcome, build, test)
    headline = ". ".join(clauses) or "Setup result unavailable"
    if tone != "success":
        headline += ". Review before promoting"
    detail = (blocker or {}).get("hint") if blocker else None
    return {
        "tone": tone,
        "headline": headline,
        "detail": detail,
        "verdict": canonical_verdict,
        "source": verdict_source,
    }

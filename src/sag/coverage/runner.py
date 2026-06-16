"""Deterministic, isolated, best-effort coverage runner.

Reuses existing jacoco.xml when present, else injects JaCoCo WITHOUT editing
project files (Maven CLI plugin goals / Gradle --init-script) and re-runs the
test suite, then parses per-module reports into a coverage map and merges it
into module_metrics.json. Any failure leaves coverage absent; it never raises
into the caller (the setup is already finished)."""

import json
from typing import Any, Dict, Optional

from loguru import logger

from sag.coverage.jacoco_parser import parse_jacoco_xml
from sag.coverage.merge import merge_coverage_into_metrics
from sag.tools.module_metrics import MODULE_METRICS_PATH

JACOCO_VERSION = "0.8.12"
COVERAGE_TIMEOUT_SEC = 1800

# Gradle init script: apply jacoco to all projects + force an XML report. No
# build.gradle edits; passed via --init-script only.
_GRADLE_INIT = """allprojects { p ->
    p.plugins.withId('java') { p.apply plugin: 'jacoco' }
    p.tasks.withType(JacocoReport).configureEach { reports.xml.required = true }
}
"""


def _find_reports(orchestrator: Any, project_dir: str, build_system: str) -> list:
    if build_system == "gradle":
        cmd = f"find {project_dir} -path '*/build/reports/jacoco/*' -name 'jacoco*.xml' 2>/dev/null"
    else:
        cmd = f"find {project_dir} -path '*/target/site/jacoco/*' -name 'jacoco.xml' 2>/dev/null"
    res = orchestrator.execute_command(cmd)
    return [l for l in (res.get("output") or "").splitlines() if l.strip().endswith(".xml")]


def _module_path(project_dir: str, xml_path: str, build_system: str) -> str:
    # .../<module>/target/site/jacoco/jacoco.xml  or  .../<module>/build/reports/jacoco/.../*.xml
    marker = "/build/" if build_system == "gradle" else "/target/"
    head = xml_path.split(marker)[0]
    rel = head[len(project_dir):].strip("/")
    return rel or "."


def _inject_and_run(orchestrator: Any, project_dir: str, build_system: str) -> None:
    if build_system == "gradle":
        init_path = f"{project_dir}/.setup_agent_jacoco.init.gradle"
        delim = "SAG_JACOCO_INIT"
        orchestrator.execute_command(
            f"cat > {init_path} <<'{delim}'\n{_GRADLE_INIT}\n{delim}"
        )
        cmd = (
            f"cd {project_dir} && (./gradlew --no-daemon --continue "
            f"--init-script {init_path} test jacocoTestReport "
            f"|| gradle --no-daemon --continue --init-script {init_path} test jacocoTestReport)"
        )
    else:
        plugin = f"org.jacoco:jacoco-maven-plugin:{JACOCO_VERSION}"
        cmd = (
            f"cd {project_dir} && mvn -B {plugin}:prepare-agent test {plugin}:report "
            f"-Dmaven.test.failure.ignore=true"
        )
    orchestrator.execute_command(cmd, timeout=COVERAGE_TIMEOUT_SEC)


def run_coverage(
    orchestrator: Any, project_dir: str, build_system: Optional[str] = None
) -> Dict[str, Dict[str, Any]]:
    """Produce a {reactor_path: coverage} map. Best-effort: {} on any failure."""
    try:
        if build_system is None:
            return {}
        existing = _find_reports(orchestrator, project_dir, build_system)
        source = "jacoco-existing"
        if not existing:
            _inject_and_run(orchestrator, project_dir, build_system)
            existing = _find_reports(orchestrator, project_dir, build_system)
            source = "jacoco-injected"

        coverage: Dict[str, Dict[str, Any]] = {}
        for xml_path in existing:
            cat = orchestrator.execute_command(f"cat '{xml_path}'")
            cov = parse_jacoco_xml(cat.get("output") or "")
            if not cov:
                continue
            path = _module_path(project_dir, xml_path, build_system)
            cov["coverage_source"] = source
            # If multiple reports map to one module, keep the larger line_total.
            prev = coverage.get(path)
            if prev is None or (cov.get("line_total") or 0) >= (prev.get("line_total") or 0):
                coverage[path] = cov
        return coverage
    except Exception as exc:  # never propagate; setup already finished
        logger.warning(f"Coverage run failed (best-effort, ignored): {exc}")
        return {}


def apply_coverage(orchestrator: Any, project_dir: str, build_system: Optional[str] = None) -> bool:
    """Run coverage and merge it into module_metrics.json in the container.
    Returns True when coverage was written, False otherwise (best-effort)."""
    coverage = run_coverage(orchestrator, project_dir, build_system)
    if not coverage:
        return False
    try:
        cat = orchestrator.execute_command(f"cat {MODULE_METRICS_PATH}")
        if not cat.get("success") or not (cat.get("output") or "").strip():
            return False
        metrics = json.loads(cat["output"])
        merged = merge_coverage_into_metrics(metrics, coverage)
        payload = json.dumps(merged, indent=2)
        delim = "SAG_MODULE_METRICS_EOF"
        orchestrator.execute_command(
            f"cat > {MODULE_METRICS_PATH} <<'{delim}'\n{payload}\n{delim}"
        )
        return True
    except Exception as exc:
        logger.warning(f"Coverage merge/write failed (best-effort, ignored): {exc}")
        return False

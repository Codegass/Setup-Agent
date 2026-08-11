"""Deterministic, isolated, best-effort coverage runner.

Reuses existing jacoco.xml when present, else injects JaCoCo WITHOUT editing
project files (Maven CLI plugin goals / Gradle --init-script) and re-runs the
test suite, then parses per-module reports into a coverage map and merges it
into module_metrics.json. Any failure leaves coverage absent; it never raises
into the caller (the setup is already finished)."""

import json
import posixpath
import shlex
from typing import Any, Dict, Optional

from loguru import logger

from sag.coverage.jacoco_parser import parse_jacoco_xml
from sag.coverage.merge import merge_coverage_into_metrics
from sag.runtime.container_io import read_container_text, resolve_control_execute
from sag.tools.module_metrics import MODULE_METRICS_PATH
from sag.utils.container_io import compare_publish_container_text_atomic

JACOCO_VERSION = "0.8.12"
COVERAGE_TIMEOUT_SEC = 1800

# Gradle init script: apply jacoco to all projects + force an XML report. No
# build.gradle edits; passed via --init-script only.
_GRADLE_INIT = """allprojects { p ->
    p.plugins.withId('java') { p.apply plugin: 'jacoco' }
    p.tasks.withType(JacocoReport).configureEach { reports.xml.required = true }
}
"""


class _CoverageControlFailure(RuntimeError):
    """A required clean-control operation was not explicitly successful."""


def _execute_control(orchestrator: Any, command: str, **kwargs: Any) -> dict[str, Any]:
    execute = resolve_control_execute(orchestrator)
    if execute is None:
        return {
            "success": False,
            "exit_code": -1,
            "output": "clean control transport unavailable",
            "dispatch_status": "control_transport_unavailable",
            "runner_dispatched": False,
        }
    return dict(execute(command, **kwargs) or {})


def _execute_control_required(
    orchestrator: Any,
    command: str,
    *,
    operation: str,
    **kwargs: Any,
) -> dict[str, Any]:
    # Coverage discovery and XML/JSON reads are machine evidence. The normal
    # Docker presentation path may smart-truncate JSON or large single-line
    # XML, so every clean-control observation is explicitly lossless.
    kwargs.setdefault("truncate_output", False)
    result = _execute_control(orchestrator, command, **kwargs)
    if result.get("success") is not True or result.get("exit_code") != 0:
        raise _CoverageControlFailure(
            f"{operation} failed: {result.get('output') or result.get('dispatch_status') or 'unknown error'}"
        )
    return result


def _canonical_absolute_path(path: Any, *, label: str) -> str:
    if not isinstance(path, str) or not path or not posixpath.isabs(path):
        raise _CoverageControlFailure(f"{label} must be an absolute path")
    if posixpath.normpath(path) != path:
        raise _CoverageControlFailure(f"{label} must be canonical")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise _CoverageControlFailure(f"{label} contains control characters")
    if "'" in path or '"' in path:
        raise _CoverageControlFailure(f"{label} contains a forbidden quote")
    return path


def _canonical_project_root(project_dir: Any) -> str:
    root = _canonical_absolute_path(project_dir, label="coverage project root")
    if root == "/":
        raise _CoverageControlFailure("coverage project root cannot be the filesystem root")
    return root


def _contained_path(path: Any, *, project_root: str, label: str) -> str:
    canonical = _canonical_absolute_path(path, label=label)
    try:
        contained = posixpath.commonpath([project_root, canonical]) == project_root
    except ValueError as exc:
        raise _CoverageControlFailure(f"{label} is not comparable to the project root") from exc
    if not contained or canonical == project_root:
        raise _CoverageControlFailure(f"{label} is outside the project root")
    return canonical


def _parse_find_paths(
    output: Any,
    *,
    project_root: str,
    build_system: str,
    report_paths: bool,
) -> list[str]:
    if not isinstance(output, str):
        raise _CoverageControlFailure("coverage find output was not text")
    if not output:
        return []

    # Production find uses -print0, so a newline can only be part of a path and
    # is rejected. A single non-NUL path remains accepted for small legacy unit
    # doubles; multiple newline-delimited paths are deliberately not accepted.
    if "\x00" in output:
        if not output.endswith("\x00"):
            raise _CoverageControlFailure("coverage find output was not NUL terminated")
        raw_paths = output[:-1].split("\x00")
        if any(not path for path in raw_paths):
            raise _CoverageControlFailure("coverage find output contained an empty record")
    else:
        if "\n" in output or "\r" in output:
            raise _CoverageControlFailure("coverage find output was not NUL delimited")
        raw_paths = [output]

    paths: list[str] = []
    seen: set[str] = set()
    for raw_path in raw_paths:
        path = _contained_path(
            raw_path,
            project_root=project_root,
            label="coverage report path" if report_paths else "JaCoCo exec path",
        )
        if report_paths:
            if build_system == "gradle":
                valid_shape = (
                    "/build/reports/jacoco/" in path
                    and posixpath.basename(path).startswith("jacoco")
                    and path.endswith(".xml")
                )
            else:
                valid_shape = (
                    "/target/site/jacoco/" in path and posixpath.basename(path) == "jacoco.xml"
                )
            if not valid_shape:
                raise _CoverageControlFailure("coverage report path has an invalid shape")
        elif posixpath.basename(path) != "jacoco.exec":
            raise _CoverageControlFailure("JaCoCo exec path has an invalid shape")
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _find_reports(orchestrator: Any, project_dir: str, build_system: str) -> list[str]:
    project_root = _canonical_project_root(project_dir)
    if build_system == "gradle":
        path_pattern = "*/build/reports/jacoco/*"
        name_pattern = "jacoco*.xml"
    else:
        path_pattern = "*/target/site/jacoco/*"
        name_pattern = "jacoco.xml"
    cmd = (
        f"find {shlex.quote(project_root)} -path {shlex.quote(path_pattern)} "
        f"-name {shlex.quote(name_pattern)} -type f -print0 2>/dev/null"
    )
    result = _execute_control_required(
        orchestrator,
        cmd,
        operation="coverage report discovery",
    )
    return _parse_find_paths(
        result.get("output", ""),
        project_root=project_root,
        build_system=build_system,
        report_paths=True,
    )


def _module_path(project_dir: str, xml_path: str, build_system: str) -> str:
    # .../<module>/target/site/jacoco/jacoco.xml  or  .../<module>/build/reports/jacoco/.../*.xml
    project_root = _canonical_project_root(project_dir)
    report_path = _contained_path(
        xml_path,
        project_root=project_root,
        label="coverage report path",
    )
    relative_report = posixpath.relpath(report_path, project_root)
    marker = "/build/" if build_system == "gradle" else "/target/"
    module_path, separator, _ = f"/{relative_report}".rpartition(marker)
    if not separator:
        raise _CoverageControlFailure(
            f"coverage report path lacks the expected {build_system} output marker"
        )
    return module_path.removeprefix("/") or "."


def _has_jacoco_exec(orchestrator: Any, project_dir: str) -> bool:
    """True when the setup's test run already produced JaCoCo exec data (the
    project configures its own JaCoCo). Then we materialize a report instead of
    injecting a conflicting second agent."""
    project_root = _canonical_project_root(project_dir)
    result = _execute_control_required(
        orchestrator,
        (
            f"find {shlex.quote(project_root)} -name {shlex.quote('jacoco.exec')} "
            "-type f -print0 -quit 2>/dev/null"
        ),
        operation="JaCoCo exec discovery",
    )
    return bool(
        _parse_find_paths(
            result.get("output", ""),
            project_root=project_root,
            build_system="maven",
            report_paths=False,
        )
    )


def _maven_report_only(orchestrator: Any, project_dir: str) -> None:
    """Generate JaCoCo XML from existing exec data (no prepare-agent, no re-run)."""
    project_root = _canonical_project_root(project_dir)
    plugin = f"org.jacoco:jacoco-maven-plugin:{JACOCO_VERSION}"
    cmd = (
        f"cd {shlex.quote(project_root)} && mvn -B {plugin}:report "
        "-Dmaven.test.failure.ignore=true"
    )
    orchestrator.execute_command(cmd, timeout=COVERAGE_TIMEOUT_SEC)


def _inject_and_run(orchestrator: Any, project_dir: str, build_system: str) -> None:
    project_root = _canonical_project_root(project_dir)
    if build_system == "gradle":
        init_path = _contained_path(
            posixpath.join(project_root, ".setup_agent_jacoco.init.gradle"),
            project_root=project_root,
            label="Gradle coverage init path",
        )
        delim = "SAG_JACOCO_INIT"
        _execute_control_required(
            orchestrator,
            f"cat > {shlex.quote(init_path)} <<'{delim}'\n{_GRADLE_INIT}\n{delim}",
            operation="Gradle coverage init write",
        )
        cmd = (
            f"cd {shlex.quote(project_root)} && (./gradlew --no-daemon --continue "
            f"--init-script {shlex.quote(init_path)} test jacocoTestReport "
            f"|| gradle --no-daemon --continue --init-script {shlex.quote(init_path)} "
            "test jacocoTestReport)"
        )
    else:
        plugin = f"org.jacoco:jacoco-maven-plugin:{JACOCO_VERSION}"
        cmd = (
            f"cd {shlex.quote(project_root)} && mvn -B {plugin}:prepare-agent test "
            f"{plugin}:report "
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
        project_root = _canonical_project_root(project_dir)
        existing = _find_reports(orchestrator, project_root, build_system)
        source = "jacoco-existing"
        if (
            not existing
            and build_system == "maven"
            and _has_jacoco_exec(orchestrator, project_root)
        ):
            # The project configures its OWN JaCoCo: the setup's test run already
            # produced jacoco.exec but no XML. Materialize the XML from that exec
            # data with a report-only goal. Do NOT inject a second prepare-agent:
            # two -javaagent JaCoCo agents collide and crash tests with a
            # StackOverflowError (live commons-cli, which ships jacoco 0.8.15).
            _maven_report_only(orchestrator, project_root)
            existing = _find_reports(orchestrator, project_root, build_system)
        if not existing:
            _inject_and_run(orchestrator, project_root, build_system)
            existing = _find_reports(orchestrator, project_root, build_system)
            source = "jacoco-injected"

        coverage: Dict[str, Dict[str, Any]] = {}
        for xml_path in existing:
            cat = _execute_control_required(
                orchestrator,
                f"cat {shlex.quote(xml_path)}",
                operation="JaCoCo XML read",
            )
            cov = parse_jacoco_xml(cat.get("output") or "")
            if not cov:
                continue
            path = _module_path(project_root, xml_path, build_system)
            cov["coverage_source"] = source
            # If multiple reports map to one module, keep the larger line_total.
            prev = coverage.get(path)
            if prev is None or (cov.get("line_total") or 0) >= (prev.get("line_total") or 0):
                coverage[path] = cov
        return coverage
    except Exception as exc:  # never propagate; setup already finished
        logger.warning(f"Coverage run failed (best-effort, ignored): {exc}")
        return {}


def apply_coverage(
    orchestrator: Any,
    project_dir: str,
    build_system: Optional[str] = None,
    *,
    baseline_metrics: Optional[Dict[str, Any]] = None,
) -> bool:
    """Run coverage and merge it into module_metrics.json in the container.
    Returns True when coverage was written, False otherwise (best-effort)."""
    try:
        metrics_raw = read_container_text(
            orchestrator,
            MODULE_METRICS_PATH,
            exact_bytes=True,
        )
        if metrics_raw is None:
            # Setup coverage runs before ReportTool has emitted
            # module_metrics.json.  The physical build gate already owns one
            # cached module scan; use that exact object as the absent-file CAS
            # baseline instead of scanning again or skipping JaCoCo entirely.
            if (
                not isinstance(baseline_metrics, dict)
                or not isinstance(baseline_metrics.get("module_summary"), dict)
                or not isinstance(baseline_metrics.get("modules"), list)
                or not baseline_metrics["modules"]
            ):
                return False
            metrics = json.loads(json.dumps(baseline_metrics))
        else:
            if not metrics_raw.strip():
                return False
            metrics = json.loads(metrics_raw)
        coverage = run_coverage(orchestrator, project_dir, build_system)
        if not coverage:
            return False
        merged = merge_coverage_into_metrics(metrics, coverage)
        payload = json.dumps(merged, indent=2)
        write = compare_publish_container_text_atomic(
            orchestrator,
            MODULE_METRICS_PATH,
            payload,
            expected_content=metrics_raw,
            validate_json=True,
        )
        return write.persisted
    except Exception as exc:
        logger.warning(f"Coverage merge/write failed (best-effort, ignored): {exc}")
        return False

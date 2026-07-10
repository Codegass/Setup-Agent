"""Report tool for generating task summaries and marking completion."""

import json
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from loguru import logger

from sag import __version__
from sag.agent.context_manager import TaskStatus
from sag.agent.phase_machine import CRITICAL_PHASE
from sag.agent.physical_validator import evaluate_run_verdict
from sag.config.settings import DEFAULT_TEST_EXECUTION_THRESHOLD, DEFAULT_TEST_PASS_THRESHOLD
from sag.evidence import TestStats, aggregate_evidence_status, coerce_evidence_status
from sag.reporting import format_percentage, render_condensed_summary, truncate_list
from sag.runtime.env_overlay import EnvOverlayStore
from sag.tools.module_metrics import MODULE_METRICS_PATH, assemble_module_metrics

# Sentinel for memoizing _build_module_metrics (the result can legitimately be
# None, so None cannot double as "not computed yet").
_MODULE_METRICS_UNSET = object()
from sag.ui.events import EventType, UIEventEmitter
from sag.verdict import combine_verdicts, run_verdict

from .base import BaseTool, ToolResult

MAX_RUNTIME_ENV_OVERLAY_BLOCKED_ROWS = 5
MAX_RUNTIME_ENV_OVERLAY_REASON_CHARS = 160


# Local status vocabularies mapped onto the verdict kernel's closed vocabulary
# (spec §6: failed < partial < success). Statuses outside the map (e.g.
# "unknown") abstain — the kernel treats them as "no objection raised".
KERNEL_VERDICT_BY_REPORT_STATUS = {
    "success": "success",
    "partial": "partial",
    "conflict": "partial",
    "fail": "failed",
    "failed": "failed",
    "failure": "failed",
    "error": "failed",
    "blocked": "failed",
}


def build_stored_test_analysis(test_analysis: Dict[str, Any]) -> Dict[str, Any]:
    """Project a parser test_analysis dict onto the stored physical_validation shape.

    Carries the exact keys downstream consumers read: the singular
    ``report_file_count`` and ``failing_test_names`` that assemble_report_metrics
    reads (alongside the legacy plural ``report_files_count`` alias markdown
    rendering still uses), AND the unique-normalized / raw runner counts that
    _build_report_snapshot folds into status.tests_unique / tests_total_raw.

    Without the singular keys, metrics.test.report_file_count / failing_names
    were dead (always None / []) from real runs. Without the unique_*/raw_*
    keys, every metrics.test.unique_* field was null from real runs even though
    parse_test_reports computed them -- the projection silently dropped them,
    defeating the runner-executions-vs-unique-methods distinction.
    """
    report_files = test_analysis.get("report_files") or []
    report_file_count = test_analysis.get("report_file_count")
    if not isinstance(report_file_count, int):
        report_file_count = len(report_files)
    return {
        "total_tests": test_analysis.get("total_tests"),
        "passed_tests": test_analysis.get("passed_tests"),
        "failed_tests": test_analysis.get("failed_tests"),
        "error_tests": test_analysis.get("error_tests"),
        "skipped_tests": test_analysis.get("skipped_tests"),
        # Raw runner executions (parameterized/dynamic expansions counted) --
        # _build_report_snapshot reads these into status.tests_*_raw and uses
        # raw vs unique to detect parameterized expansion.
        "raw_total_tests": test_analysis.get("raw_total_tests"),
        "raw_passed_tests": test_analysis.get("raw_passed_tests"),
        "raw_failed_tests": test_analysis.get("raw_failed_tests"),
        "raw_error_tests": test_analysis.get("raw_error_tests"),
        "raw_skipped_tests": test_analysis.get("raw_skipped_tests"),
        # Unique normalized methods (parameterized/dynamic folded) -- these feed
        # status.tests_unique / tests_*_unique that the metrics contract reads.
        "unique_tests": test_analysis.get("unique_tests"),
        "unique_passed_tests": test_analysis.get("unique_passed_tests"),
        "unique_failed_tests": test_analysis.get("unique_failed_tests"),
        "unique_error_tests": test_analysis.get("unique_error_tests"),
        "unique_skipped_tests": test_analysis.get("unique_skipped_tests"),
        # Legacy plural alias (markdown consumers) + the singular key the
        # report_metrics contract actually reads.
        "report_files_count": len(report_files),
        "report_file_count": report_file_count,
        "failing_test_names": list(test_analysis.get("failing_test_names") or []),
        "test_exclusions": test_analysis.get("test_exclusions", []),
        "modules_without_tests": test_analysis.get("modules_without_tests", []),
        # Statically discovered test total from the catalog scan. Carried so the
        # report can backfill the "detected" count when analyze never persisted
        # static_test_count to the trunk (see _build_report_snapshot).
        "catalog_test_count": test_analysis.get("catalog_test_count"),
    }


def _coerce_kernel_verdict(status: Optional[str]) -> Optional[str]:
    """Map a report/evidence status string to the kernel vocabulary."""
    if status is None:
        return None
    return KERNEL_VERDICT_BY_REPORT_STATUS.get(str(status).strip().lower())


REPORT_LEGACY_STATUS_TO_EVIDENCE_STATUS = {
    "success": "success",
    "fail": "blocked",
    "failed": "blocked",
    "failure": "blocked",
    "error": "blocked",
    "partial": "partial",
    "conflict": "conflict",
    "unknown": "unknown",
    "blocked": "blocked",
}


class ReportTool(BaseTool, UIEventEmitter):
    """
    Tool for generating comprehensive project setup reports and marking task completion.

    Enhanced Features (v2024.09):
    - Physical evidence-based validation via PhysicalValidator integration
    - Consistent report filename generation for log display and file saving
    - Safe markdown file writing using here-doc with base64 fallback
    - Unified execution metrics with phase status driven by physical validation
    - Comprehensive error analysis and next-steps recommendations

    The ReportTool now prioritizes physical evidence over log inference for accurate
    status determination, eliminating false positives and providing detailed
    validation evidence in the generated reports.
    """

    def __init__(
        self,
        docker_orchestrator=None,
        execution_history_callback=None,
        context_manager=None,
        physical_validator=None,
    ):
        BaseTool.__init__(
            self,
            name="report",
            description="Generate comprehensive project setup report and mark task as complete. "
            "Creates both console output and a Markdown file in /workspace. "
            "Use this tool when all main tasks are finished to summarize the work done.",
        )
        UIEventEmitter.__init__(self)
        self.docker_orchestrator = docker_orchestrator
        self.execution_history_callback = execution_history_callback
        self.context_manager = context_manager
        self.physical_validator = physical_validator

    def execute(
        self,
        action: str = "generate",
        summary: Optional[str] = None,
        status: str = "success",
        details: Optional[str] = None,
        evidence_status: Optional[str] = None,
        test_stats: Optional[Dict[str, Any] | TestStats] = None,
        conflicts: Optional[List[str]] = None,
        evidence_refs: Optional[List[str]] = None,
        **kwargs,
    ) -> ToolResult:
        """
        Generate project setup report and mark completion.

        Args:
            action: Action to perform ('generate' for final report)
            summary: Brief summary of what was accomplished
            status: Overall status ('success' or 'fail') - REQUIRED
                   - 'success': Build validation passed AND test pass rate >= 80%
                   - 'fail': Build failed OR test report not found OR test pass rate < 80%
            details: Additional details about the setup process
        """
        result_test_stats = self._coerce_report_test_stats(test_stats)
        result_conflicts = list(conflicts or [])
        result_evidence_refs = list(evidence_refs or [])
        result_evidence_status = self._coerce_report_evidence_status(evidence_status, status)

        # IDEMPOTENCY CHECK: Prevent multiple report generation
        # Check if a report was already generated recently (within last 5 minutes)
        if action == "generate" and self.docker_orchestrator:
            try:
                # Check for ANY existing reports (broader check to prevent duplicates)
                check_cmd = "find /workspace -maxdepth 1 -name '*report*.md' -type f 2>/dev/null | grep -E '(setup.*report|final.*report|report.*md)' | head -1"
                result = self.docker_orchestrator.execute_command(check_cmd)
                existing_report = result.get("output", "").strip()

                if existing_report:
                    # A report was already generated
                    logger.warning(
                        f"Report already exists at {existing_report}, skipping duplicate generation"
                    )
                    completed_context_task = self._mark_final_report_task_completed(
                        existing_report, status
                    )

                    # Read and return the existing report content
                    read_cmd = f"head -100 {existing_report}"
                    read_result = self.docker_orchestrator.execute_command(read_cmd)
                    existing_content = (
                        read_result.get("output", "") if read_result.get("success") else ""
                    )

                    # Return the information about the existing report
                    return ToolResult(
                        success=True,
                        output=self._append_evidence_summary_to_output(
                            f"📄 Report already generated: {existing_report}\n"
                            f"Status: {status.upper()}\n"
                            f"(Using existing report to prevent duplicates)\n\n"
                            f"{existing_content[:500]}...",
                            result_evidence_status.value,
                            result_test_stats,
                            result_conflicts,
                        ),
                        status=result_evidence_status,
                        test_stats=result_test_stats,
                        conflicts=result_conflicts,
                        evidence_refs=result_evidence_refs,
                        metadata={
                            "task_completed": True,
                            "completion_signal": True,
                            "status": status,
                            "final_flow_status": status,
                            "evidence_status": result_evidence_status.value,
                            "existing_report": existing_report,
                            "duplicate_prevention": True,
                            "context_task_completed": completed_context_task,
                            "test_stats": (
                                self._serialize_report_test_stats(result_test_stats)
                                if result_test_stats
                                else None
                            ),
                            "conflicts": result_conflicts,
                            "evidence_refs": result_evidence_refs,
                        },
                    )
            except Exception as e:
                logger.debug(f"Failed to check for existing reports: {e}")
                # Continue with report generation if check fails

        # Check for unexpected parameters
        if kwargs:
            invalid_params = list(kwargs.keys())
            return ToolResult(
                success=False,
                output=(
                    f"❌ Invalid parameters for report tool: {invalid_params}\n\n"
                    f"✅ Valid parameters:\n"
                    f"  - action (optional): 'generate' (default: 'generate')\n"
                    f"  - summary (optional): Brief summary of accomplishments\n"
                    f"  - status (required): 'success' or 'fail'\n"
                    f"     • 'success': Build passed AND test pass rate >= 80%\n"
                    f"     • 'fail': Build failed OR tests not found OR pass rate < 80%\n"
                    f"  - details (optional): Additional details about the setup\n\n"
                    f"  - evidence_status (optional): success/partial/blocked/conflict/unknown\n"
                    f"  - test_stats (optional): TestStats or test counts dictionary\n"
                    f"  - conflicts (optional): List of evidence conflicts\n"
                    f"  - evidence_refs (optional): List of traceable evidence references\n\n"
                    f"Example: report(action='generate')\n"
                    f"Example: report(action='generate', summary='Project built successfully', status='success')\n"
                    f"Example: report(action='generate', summary='Project built successfully', status='success', details='All build and test tasks completed successfully')"
                ),
                error=f"Invalid parameters: {invalid_params}",
            )

        if not status:
            return ToolResult(
                success=False,
                output="❌ Missing required parameter: 'status'. Must be either 'success' or 'fail'\n"
                "• 'success': Build passed AND test pass rate >= 80%\n"
                "• 'fail': Build failed OR tests not found OR pass rate < 80%",
                error="Missing required parameter: status",
            )

        logger.info(f"Generating project report with status: {status}")

        try:
            if action == "generate":
                # CRITICAL: Verify all prerequisite tasks are completed before generating report
                context_validation = self._validate_context_prerequisites()
                if not context_validation["valid"]:
                    return ToolResult(
                        success=False,
                        output="",
                        error=context_validation["error"],
                        suggestions=context_validation["suggestions"],
                        error_code="PREREQUISITE_TASKS_INCOMPLETE",
                    )

                (
                    report,
                    verified_status,
                    report_filename,
                    actual_accomplishments,
                    report_snapshot,
                ) = self._generate_comprehensive_report(
                    summary,
                    status,
                    details,
                    evidence_status=evidence_status,
                    test_stats=result_test_stats,
                    conflicts=conflicts,
                    evidence_refs=evidence_refs,
                )
                completed_context_task = self._mark_final_report_task_completed(
                    report_filename, verified_status
                )

                evidence_result = report_snapshot.get("evidence_result", {})
                result_evidence_status = self._coerce_report_evidence_status(
                    evidence_result.get("status"),
                    evidence_status,
                    verified_status,
                    status,
                )
                if test_stats is None:
                    result_test_stats = self._coerce_report_test_stats(
                        evidence_result.get("test_stats")
                    )
                if conflicts is None:
                    result_conflicts = list(evidence_result.get("conflicts") or [])
                if evidence_refs is None:
                    result_evidence_refs = list(evidence_result.get("evidence_refs") or [])

                # Mark this as a completion signal for the ReAct engine
                metadata = {
                    "task_completed": True,
                    "completion_signal": True,
                    "status": status,
                    "final_flow_status": status,
                    "verified_status": verified_status,  # Include the verified status
                    "evidence_status": result_evidence_status.value,
                    "timestamp": datetime.now().isoformat(),
                    "report_snapshot": report_snapshot,
                    "context_task_completed": completed_context_task,
                }
                if result_test_stats:
                    metadata["test_stats"] = self._serialize_report_test_stats(result_test_stats)
                if result_conflicts:
                    metadata["conflicts"] = result_conflicts
                if result_evidence_refs:
                    metadata["evidence_refs"] = result_evidence_refs

                # ENHANCED: Provide condensed output for logs to reduce noise
                # Full report is saved to markdown file, logs get summary only
                condensed_output = self._generate_condensed_log_output(
                    verified_status,
                    report_filename,
                    actual_accomplishments,
                    report_snapshot,
                )
                condensed_output = self._append_evidence_summary_to_output(
                    condensed_output,
                    metadata["evidence_status"],
                    result_test_stats,
                    result_conflicts,
                )

                # Emit UI event for report generation
                self.emit(
                    EventType.REPORT_GENERATED,
                    message=f"Report generated: {report_filename}",
                    report_path=f"/workspace/{report_filename}",
                    status=verified_status,
                    build_success=actual_accomplishments.get("build_success", False),
                    test_success=actual_accomplishments.get("test_success", False),
                    test_pass_rate=actual_accomplishments.get("physical_validation", {})
                    .get("test_analysis", {})
                    .get("pass_rate", 0),
                    total_tests=actual_accomplishments.get("physical_validation", {})
                    .get("test_analysis", {})
                    .get("total_tests", 0),
                    passed_tests=actual_accomplishments.get("physical_validation", {})
                    .get("test_analysis", {})
                    .get("passed_tests", 0),
                )

                return ToolResult(
                    success=True,
                    output=condensed_output,
                    status=metadata["evidence_status"],
                    metadata=metadata,
                    documentation_links=[],
                    test_stats=result_test_stats,
                    conflicts=result_conflicts,
                    evidence_refs=result_evidence_refs,
                    raw_data={
                        "full_report": report,
                        "report_snapshot": report_snapshot,
                        "final_flow_status": status,
                        "verified_status": verified_status,
                        "evidence_status": metadata["evidence_status"],
                        "test_stats": (
                            self._serialize_report_test_stats(result_test_stats)
                            if result_test_stats
                            else None
                        ),
                        "conflicts": result_conflicts,
                        "evidence_refs": result_evidence_refs,
                    },  # Store full report outside condensed metadata
                )
            else:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Invalid action '{action}'. Use 'generate' to create report.",
                    suggestions=["Use action='generate' to create the final report"],
                )

        except Exception as e:
            logger.error(f"Failed to generate report: {e}")
            return ToolResult(
                success=False,
                output="",
                error=f"Report generation failed: {str(e)}",
                suggestions=["Check if all required information is available"],
            )

    def _coerce_report_test_stats(
        self, test_stats: Optional[Dict[str, Any] | TestStats]
    ) -> Optional[TestStats]:
        """Normalize report test stats into the shared evidence model."""
        if test_stats is None:
            return None
        if isinstance(test_stats, TestStats):
            return test_stats
        if not isinstance(test_stats, dict):
            logger.debug(f"Ignoring unsupported report test_stats value: {type(test_stats)}")
            return None

        def as_int(*keys: str, default: Optional[int] = 0) -> Optional[int]:
            for key in keys:
                value = test_stats.get(key)
                if value is None:
                    continue
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
            return default

        failed = as_int("failed", "failed_tests", default=0) or 0
        failed += as_int("error", "errors", "error_tests", default=0) or 0
        skipped = as_int("skipped", "skipped_tests", default=0) or 0
        passed = as_int("passed", "passed_tests", default=0) or 0
        executed = as_int("executed", "total", "total_tests", default=None)
        if executed is None:
            executed = passed + failed + skipped

        return TestStats(
            discovered=as_int("discovered", "static_test_count", default=None),
            executed=executed,
            passed=passed,
            failed=failed,
            skipped=skipped,
        )

    def _serialize_report_test_stats(self, test_stats: TestStats) -> Dict[str, Any]:
        data = test_stats.model_dump()
        data["pass_rate"] = test_stats.pass_rate
        execution_rate = test_stats.execution_rate
        if execution_rate is not None:
            data["execution_rate"] = execution_rate
        return data

    def _map_report_status_to_evidence_status(self, status: Any) -> str:
        """Map report legacy statuses onto shared evidence statuses."""
        if status is None:
            return "unknown"
        if hasattr(status, "value"):
            status = status.value
        normalized = str(status).strip().lower()
        if not normalized:
            return "unknown"
        return REPORT_LEGACY_STATUS_TO_EVIDENCE_STATUS.get(normalized, "unknown")

    def _coerce_report_evidence_status(self, *statuses: Any):
        for status in statuses:
            if status is None:
                continue
            raw_status = status.value if hasattr(status, "value") else status
            normalized = str(raw_status).strip().lower()
            if not normalized:
                return coerce_evidence_status(None)
            mapped = self._map_report_status_to_evidence_status(status)
            return coerce_evidence_status(mapped)
        return coerce_evidence_status(None)

    def _resolve_report_evidence_result(
        self,
        evidence_status: Optional[str],
        test_stats: Optional[Dict[str, Any] | TestStats],
        conflicts: Optional[List[str]],
        evidence_refs: Optional[List[str]],
        verified_status: str,
        claimed_status: str,
        actual_accomplishments: dict,
    ) -> Dict[str, Any]:
        """Resolve explicit report evidence with physical validator defaults."""
        defaults = self._extract_report_evidence_defaults(actual_accomplishments)

        resolved_test_stats = self._coerce_report_test_stats(test_stats)
        if resolved_test_stats is None:
            resolved_test_stats = self._coerce_report_test_stats(defaults.get("test_stats"))

        resolved_conflicts = (
            list(conflicts) if conflicts is not None else list(defaults.get("conflicts") or [])
        )
        resolved_refs = (
            list(evidence_refs)
            if evidence_refs is not None
            else list(defaults.get("evidence_refs") or [])
        )

        status_candidates: List[Any] = []
        if evidence_status is not None:
            status_candidates.append(evidence_status)
        else:
            status_candidates.extend(defaults.get("status_candidates") or [])
            derived_status = self._derive_evidence_status_from_test_stats(
                resolved_test_stats, resolved_conflicts
            )
            if derived_status:
                status_candidates.append(derived_status)
        status_candidates.extend([verified_status, claimed_status])

        if evidence_status is None and len(status_candidates) > 1:
            evidence_status_value = aggregate_evidence_status(
                self._coerce_report_evidence_status(candidate) for candidate in status_candidates
            )
        else:
            evidence_status_value = self._coerce_report_evidence_status(*status_candidates)

        serialized_stats = (
            self._serialize_report_test_stats(resolved_test_stats) if resolved_test_stats else None
        )
        return {
            "status": evidence_status_value.value,
            "test_stats": serialized_stats,
            "conflicts": resolved_conflicts,
            "evidence_refs": resolved_refs,
        }

    def _extract_report_evidence_defaults(self, actual_accomplishments: dict) -> Dict[str, Any]:
        actual_accomplishments = actual_accomplishments or {}
        physical_validation = actual_accomplishments.get("physical_validation", {}) or {}
        build_status = physical_validation.get("build_status", {}) or {}
        test_status = physical_validation.get("test_status", {}) or {}
        test_analysis = physical_validation.get("test_analysis", {}) or {}

        status_candidates = [
            actual_accomplishments.get("evidence_status"),
            build_status.get("evidence_status"),
            test_status.get("evidence_status"),
        ]

        test_stats = self._extract_report_test_stats_default(
            actual_accomplishments, test_status, test_analysis
        )

        conflicts: List[str] = []
        evidence_refs: List[str] = []
        for source in (actual_accomplishments, build_status, test_status):
            conflicts.extend(source.get("conflicts") or [])
            evidence_refs.extend(source.get("evidence_refs") or [])
        evidence_refs.extend(test_analysis.get("report_files") or [])

        return {
            "status_candidates": [status for status in status_candidates if status],
            "test_stats": test_stats,
            "conflicts": list(dict.fromkeys(conflicts)),
            "evidence_refs": list(dict.fromkeys(evidence_refs)),
        }

    def _extract_report_test_stats_default(
        self, actual_accomplishments: dict, test_status: dict, test_analysis: dict
    ) -> Optional[Dict[str, Any] | TestStats]:
        accomplishment_stats = actual_accomplishments.get("test_stats")
        if self._has_report_test_count_evidence(accomplishment_stats):
            return accomplishment_stats

        validator_stats = test_status.get("test_stats")
        if self._has_report_test_count_evidence(validator_stats):
            return validator_stats

        if self._has_report_test_count_evidence(test_analysis):
            return {
                "executed": test_analysis.get("total_tests"),
                "passed": test_analysis.get("passed_tests"),
                "failed": (test_analysis.get("failed_tests") or 0)
                + (test_analysis.get("error_tests") or 0),
                "skipped": test_analysis.get("skipped_tests"),
            }

        return None

    def _has_report_test_count_evidence(self, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, TestStats):
            return True
        if not isinstance(value, dict):
            return False

        count_keys = {
            "discovered",
            "static_test_count",
            "executed",
            "total",
            "total_tests",
            "passed",
            "passed_tests",
            "failed",
            "failed_tests",
            "errors",
            "error",
            "error_tests",
            "skipped",
            "skipped_tests",
            "raw_total_tests",
            "raw_passed_tests",
            "raw_failed_tests",
            "raw_error_tests",
            "raw_skipped_tests",
        }
        return any(key in value and value.get(key) is not None for key in count_keys)

    def _derive_evidence_status_from_test_stats(
        self, test_stats: Optional[TestStats], conflicts: List[str]
    ) -> Optional[str]:
        if not test_stats:
            return "partial" if conflicts else None
        if test_stats.failed > 0:
            return "partial" if test_stats.passed > 0 else "blocked"
        if test_stats.executed > 0:
            return "success"
        return "partial" if conflicts else None

    def _append_evidence_summary_to_output(
        self,
        output: str,
        evidence_status: str,
        test_stats: Optional[TestStats],
        conflicts: List[str],
    ) -> str:
        if evidence_status == "success" and not test_stats and not conflicts:
            return output

        lines = [output.rstrip(), "", f"Result: {evidence_status.upper()}"]
        if test_stats:
            lines.append(f"Tests: {test_stats.as_summary()}")
        if conflicts:
            lines.append(f"Conflicts: {'; '.join(conflicts)}")
        return "\n".join(lines).rstrip()

    def _render_console_evidence_result(self, snapshot: Optional[Dict[str, Any]]) -> List[str]:
        evidence = (snapshot or {}).get("evidence_result") or {}
        if not self._should_render_report_evidence_result(evidence):
            return []

        # Console Result reads the same kernel verdict as the header — round 6
        # beam printed "Result: SUCCESS" beside a FAILED header.
        lines = [f"Result: {self._snapshot_kernel_verdict(snapshot).upper()}"]
        test_stats = self._snapshot_test_stats(snapshot) or self._coerce_report_test_stats(
            evidence.get("test_stats")
        )
        if test_stats:
            lines.append(f"Tests: {test_stats.as_summary()}")
        conflicts = evidence.get("conflicts") or []
        if conflicts:
            lines.append(f"Conflicts: {'; '.join(conflicts)}")
        refs = evidence.get("evidence_refs") or []
        if refs:
            lines.append(f"Evidence refs: {'; '.join(refs)}")
        return lines

    def _render_markdown_evidence_details(
        self, evidence: Dict[str, Any], snapshot: Optional[Dict[str, Any]] = None
    ) -> List[str]:
        lines: List[str] = []
        test_stats = self._snapshot_test_stats(snapshot) or self._coerce_report_test_stats(
            evidence.get("test_stats")
        )
        if test_stats:
            lines.append(f"**Tests:** {test_stats.as_summary()}")
        conflicts = evidence.get("conflicts") or []
        if conflicts:
            lines.append(f"**Conflicts:** {'; '.join(conflicts)}")
        refs = evidence.get("evidence_refs") or []
        if refs:
            lines.append(f"**Evidence refs:** {'; '.join(refs)}")
        return lines

    def _snapshot_test_stats(self, snapshot: Optional[Dict[str, Any]]) -> Optional[TestStats]:
        """Physically-validated test stats from the report snapshot.

        The header MUST consume the same numbers as the dashboard/verdict —
        the model-supplied evidence stats are a fallback only. (06-10 eval:
        every report header contradicted its own dashboard, e.g. commons-cli
        '977/977 passed, 100%' over a dashboard showing 420/430.)
        """
        status = (snapshot or {}).get("status") or {}
        executed = status.get("tests_total")
        passed = status.get("tests_passed")
        discovered = status.get("static_test_count") or None
        # Detected-but-not-executed: when a static suite was discovered but nothing
        # ran, still surface it (executed=0) so the report says "0 of N detected
        # tests executed" instead of dropping to None and falling back to a bare
        # "0/0 passed" (Bigtop: 57 detected, 0 executed). Without a discovered count
        # there is genuinely nothing to report, so keep returning None.
        if not executed:
            if discovered:
                try:
                    return TestStats(
                        discovered=int(discovered), executed=0, passed=0, failed=0, skipped=0
                    )
                except (TypeError, ValueError):
                    return None
            return None
        if passed is None:
            return None
        try:
            return TestStats(
                discovered=discovered,
                executed=int(executed),
                passed=int(passed),
                failed=int(status.get("tests_failed", 0) or 0)
                + int(status.get("tests_errors", 0) or 0),
                skipped=int(status.get("tests_skipped", 0) or 0),
            )
        except (TypeError, ValueError):
            return None

    def _snapshot_kernel_verdict(self, snapshot: Optional[Dict[str, Any]]) -> str:
        """The run verdict for this report, via the verdict kernel (spec §6).

        Combines the same inputs the agent's final status uses — the
        phase-machine outcome (reconstructed from the trunk's phase_* tasks),
        the physically-verified overall status, the evidence status, and the
        evidence conflicts (which cap at partial). The header Result line and
        the stored snapshot verdict both read this, so the round-5 iceberg
        divergence (report PARTIAL vs CLI success) is impossible by
        construction — including machine-capped runs (round-6 review: a
        blocked phase with green physical artifacts rendered ✅ SUCCESS while
        the CLI banner said verdict=failed).
        """
        snapshot = snapshot or {}
        status = snapshot.get("status") or {}
        evidence = snapshot.get("evidence_result") or {}
        return run_verdict(
            # The kernel's two-verdict signature is kept; min() is associative,
            # so folding the machine outcome into the first input is identical
            # to a three-way combine.
            combine_verdicts(
                self._trunk_phase_machine_outcome(),
                self._physical_verdict_from_snapshot(status, snapshot),
            ),
            _coerce_kernel_verdict(evidence.get("status")),
            evidence.get("conflicts") or [],
        )

    @staticmethod
    def _physical_verdict_from_snapshot(
        status: Dict[str, Any], snapshot: Dict[str, Any]
    ) -> Optional[str]:
        """Map the snapshot's physical state to the kernel using the SAME
        tri-state rule the agent uses (round-6 beam: the legacy 'fail' for
        'build green, expected tests missing' rendered ❌ FAILED while the
        agent honestly said partial — two mappers for one physical situation).

        build green + no test results = PARTIAL (build verified, tests not),
        never failed; everything else keeps the legacy coercion."""
        coerced = _coerce_kernel_verdict(status.get("overall"))
        if coerced != "failed":
            return coerced
        build_green = bool((snapshot.get("phases") or {}).get("build"))
        tests_total = status.get("tests_total")
        if build_green and not tests_total:
            return "partial"
        return coerced

    def _trunk_phase_machine_outcome(self) -> Optional[str]:
        """Phase-machine outcome reconstructed from the trunk's phase_* tasks.

        Mirrors the agent's machine input (PhaseMachine.overall_outcome): the
        engine persists every finished phase as a phase_<name> trunk task —
        FAILED means blocked — so a blocked build phase fails the run and any
        other blocked phase caps at partial. Runs without phase_* tasks
        (`sag run --task`, legacy) abstain with None.
        """
        if not self.context_manager:
            return None
        try:
            trunk = self.context_manager.load_trunk_context()
        except Exception as exc:
            logger.debug(f"Could not load trunk for phase-machine outcome: {exc}")
            return None
        blocked = set()
        seen_phase_tasks = False
        for task in getattr(trunk, "todo_list", None) or []:
            task_id = str(getattr(task, "id", "") or "")
            if not task_id.startswith("phase_"):
                continue
            seen_phase_tasks = True
            status = getattr(task, "status", None)
            if str(getattr(status, "value", status)) == "failed":
                blocked.add(task_id[len("phase_") :])
        if not seen_phase_tasks:
            return None
        if CRITICAL_PHASE in blocked:
            return "failed"
        if blocked:
            return "partial"
        return "success"

    def _should_render_report_evidence_result(self, evidence: Dict[str, Any]) -> bool:
        if not evidence:
            return False
        test_stats = self._coerce_report_test_stats(evidence.get("test_stats"))
        conflicts = evidence.get("conflicts") or []
        refs = evidence.get("evidence_refs") or []
        status = str(evidence.get("status", "unknown")).strip().lower()
        if status == "success" and not test_stats and not conflicts and not refs:
            return False
        return True

    def _mark_final_report_task_completed(
        self, report_filename_or_path: str, verified_status: str
    ) -> Optional[str]:
        """Synchronize the final report TODO item after a report artifact is written."""
        if not self.context_manager:
            return None

        try:
            trunk_context = self.context_manager.load_trunk_context()
        except Exception as exc:
            logger.debug(f"Failed to load context while finalizing report task: {exc}")
            return None

        if not trunk_context or not getattr(trunk_context, "todo_list", None):
            return None

        report_path = self._normalize_report_path(report_filename_or_path)
        target = self._find_final_report_task(trunk_context.todo_list)
        if target is None:
            return None

        summary = "Final setup report generated."
        key_results = f"report_path={report_path}; report_status={verified_status}"
        trunk_context.update_task_status(target.id, TaskStatus.COMPLETED, summary)
        trunk_context.update_task_key_results(target.id, key_results)
        self.context_manager._save_trunk_context(trunk_context)

        # Do NOT release current_task_id for phase tasks: in phase mode the report
        # phase is closed by an explicit phase(action='done') call, not by writing
        # the report artifact. The phase machine / ReActEngine owns the phase
        # lifecycle (see test_final_report_does_not_close_active_phase_branch_before_phase_done).
        # Non-phase runs (legacy branch/trunk) still release here so the trunk
        # doesn't carry a stale current_task_id.
        if getattr(self.context_manager, "current_task_id", None) == target.id and not str(
            target.id
        ).startswith("phase_"):
            self.context_manager.current_task_id = None

        logger.info(f"Marked final report task complete: {target.id}")
        return target.id

    def _find_final_report_task(self, tasks: list[Any]) -> Optional[Any]:
        incomplete = []
        for task in tasks:
            status = getattr(getattr(task, "status", None), "value", getattr(task, "status", ""))
            if str(status).lower() == TaskStatus.COMPLETED.value:
                continue
            description = str(getattr(task, "description", "") or getattr(task, "title", ""))
            if self._is_final_report_task_description(description):
                incomplete.append(task)
        return incomplete[-1] if incomplete else None

    def _is_final_report_task_description(self, description: str) -> bool:
        text = description.lower()
        return "report" in text and any(
            marker in text for marker in ("final", "completion", "comprehensive", "setup")
        )

    def _normalize_report_path(self, report_filename_or_path: str) -> str:
        text = str(report_filename_or_path or "").strip()
        if not text:
            return "/workspace/setup-report.md"
        if text.startswith("/workspace/"):
            return text
        return f"/workspace/{text}"

    def _generate_comprehensive_report(
        self,
        summary: str,
        status: str,
        details: str,
        evidence_status: Optional[str] = None,
        test_stats: Optional[Dict[str, Any] | TestStats] = None,
        conflicts: Optional[List[str]] = None,
        evidence_refs: Optional[List[str]] = None,
    ) -> Tuple[str, str, str, dict, dict]:
        """Generate a comprehensive project setup report."""

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Generate consistent report filename for both display and saving
        report_filename = f"setup-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"

        # Get project information if available
        project_info = self._get_project_info()

        # Collect execution metrics
        execution_metrics = self._collect_execution_metrics()

        # Verify execution history and adjust status/summary if needed
        verified_status, actual_accomplishments = self._verify_execution_history(status, summary)
        report_evidence_result = self._resolve_report_evidence_result(
            evidence_status,
            test_stats,
            conflicts,
            evidence_refs,
            verified_status,
            status,
            actual_accomplishments,
        )

        # Ensure the report action itself is reflected as a successful execution
        self._finalize_report_metrics(execution_metrics, verified_status)

        report_snapshot = self._build_report_snapshot(
            verified_status,
            report_filename,
            project_info or {},
            actual_accomplishments,
            execution_metrics,
            report_evidence_result,
        )

        # Compute module metrics once, up front (memoized), and stash the
        # built/detected counts onto the snapshot status so the dashboard, the
        # submodule breakdown, and the condensed log all show the SAME
        # build-completeness numbers (detected = active reactor modules).
        try:
            module_metrics = self._build_module_metrics(
                (execution_metrics or {}).get("test_history")
                or self._load_test_history()
                or {},
                generated_at=timestamp,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"module metrics precompute skipped: {exc}")
            module_metrics = None
        if module_metrics:
            msum = module_metrics.get("module_summary") or {}
            report_snapshot["status"]["modules_detected"] = msum.get("modules_total")
            report_snapshot["status"]["modules_built"] = msum.get("modules_built")
            report_snapshot["status"]["modules_failed_count"] = msum.get("modules_failed")
            report_snapshot["status"]["modules_skipped_count"] = msum.get("modules_skipped")
            report_snapshot["status"]["modules_tested"] = msum.get("modules_tested")
            report_snapshot["status"]["modules_not_tested"] = msum.get("modules_not_tested")
            report_snapshot["status"]["modules_test_bearing"] = msum.get("modules_test_bearing")

        # Generate both console and markdown versions with verified information and metrics
        console_report = self._generate_console_report(
            summary,
            verified_status,
            details,
            timestamp,
            project_info,
            actual_accomplishments,
            execution_metrics,
            report_snapshot,
        )
        markdown_report = self._generate_markdown_report(
            summary,
            verified_status,
            details,
            timestamp,
            project_info,
            actual_accomplishments,
            execution_metrics,
            report_snapshot,
        )

        # Save markdown report to workspace with consistent filename
        self._save_markdown_report(markdown_report, timestamp, report_filename)
        try:
            from sag.tools.report_metrics import assemble_report_metrics

            physical_validation = actual_accomplishments.get("physical_validation", {}) or {}
            build_status = physical_validation.get("build_status", {}) or {}
            # Build system / fingerprint details live under build_status["evidence"];
            # there is no dedicated "build_evidence" key (verified against the
            # physical validator + report snapshot shapes).
            build_evidence = build_status.get("evidence", {}) or {}
            test_analysis = physical_validation.get("test_analysis", {}) or {}

            self._persist_report_metrics(
                assemble_report_metrics(
                    snapshot=report_snapshot,
                    build_evidence=build_evidence,
                    test_analysis=test_analysis,
                    conflicts=list(report_evidence_result.get("conflicts") or []),
                    evidence_refs=list(report_evidence_result.get("evidence_refs") or []),
                    generated_at=timestamp,
                    execution_metrics=execution_metrics,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Skipped report metrics artifact: {exc}")

        try:
            module_metrics = self._build_module_metrics(
                (execution_metrics or {}).get("test_history")
                or self._load_test_history()
                or {},
                generated_at=timestamp,
            )
            if module_metrics:
                self._persist_module_metrics(module_metrics)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"module metrics step skipped: {exc}")

        return (
            console_report,
            verified_status,
            report_filename,
            actual_accomplishments,
            report_snapshot,
        )

    def _finalize_report_metrics(self, execution_metrics: dict, verified_status: str) -> None:
        """Ensure the metrics snapshot accounts for the report tool invocation."""
        if not execution_metrics:
            return

        # Only force success accounting when the overall run verified as success
        if (verified_status or "").lower() != "success":
            return

        tools_used = execution_metrics.setdefault("tools_used", {})
        tool_failures = execution_metrics.setdefault("tool_failures", {})

        # Make sure the report tool shows up in aggregated usage
        tools_used["report"] = max(1, tools_used.get("report", 0))
        tool_failures.pop("report", None)

        total_actions = execution_metrics.get("total_actions") or 0
        successful_actions = execution_metrics.get("successful_actions") or 0
        failed_actions = execution_metrics.get("failed_actions") or 0

        unresolved_actions = total_actions - successful_actions - failed_actions
        if unresolved_actions > 0:
            execution_metrics["successful_actions"] = successful_actions + unresolved_actions

        # Recalculate success rate after adjustments
        total_actions = execution_metrics.get("total_actions") or 0
        if total_actions > 0:
            execution_metrics["success_rate"] = (
                execution_metrics.get("successful_actions", 0) / total_actions
            ) * 100

    def _load_test_history(self, max_lines: int = 40, max_bytes: int = 16384) -> Dict[str, Any]:
        """Load and aggregate recent test history events from the metrics JSONL file."""
        metrics_path = "/workspace/.setup_agent/metrics/test_summary.jsonl"
        raw_lines: List[str] = []

        if self.docker_orchestrator:
            try:
                cmd = f"if [ -f {metrics_path} ]; then tail -n {max_lines} {metrics_path}; fi"
                result = self.docker_orchestrator.execute_command(cmd)
                if result.get("exit_code") == 0 and result.get("output"):
                    raw_lines = result["output"].splitlines()
            except Exception as exc:
                logger.debug(f"Failed to fetch test history via orchestrator: {exc}")

        if not raw_lines:
            try:
                with open(metrics_path, "r", encoding="utf-8") as handle:
                    raw_lines = handle.readlines()[-max_lines:]
            except FileNotFoundError:
                return {}
            except Exception as exc:
                logger.debug(f"Failed to read test history locally: {exc}")
                return {}

        history: Dict[str, Any] = {
            "ignored_lines": 0,
            "last_cmd": {},
            "aggregate": {},
            "per_module": {},
            "exclusions": {"tests": [], "modules": []},
            "failed_tests": [],
            "flags": {},
        }

        modules_seen: Dict[str, Dict[str, Any]] = {}
        aggregate_entry: Dict[str, Any] = {}
        skipped_modules: set[str] = set()
        excluded_tests: set[str] = set()
        excluded_modules: set[str] = set()
        failed_tests: set[str] = set()
        reactor_records: List[Dict[str, Any]] = []
        failed_modules: List[str] = []
        modules_expected: Optional[int] = None

        def normalize_tests(source: Dict[str, Any]) -> Dict[str, Optional[float]]:
            def cast(value: Optional[float]) -> Optional[int]:
                if value is None:
                    return None
                try:
                    value = float(value)
                    if value.is_integer():
                        return int(value)
                    return int(value)
                except (TypeError, ValueError):
                    return None

            def pick(keys: Iterable[str]) -> Optional[float]:
                for key in keys:
                    if key in source and source[key] is not None:
                        try:
                            return float(source[key])
                        except (TypeError, ValueError):
                            return None
                return None

            total = pick(["total", "tests_total", "total_tests"])
            failed = pick(["failed", "failures", "tests_failed", "tests_failures"]) or 0
            errors = pick(["errors", "error", "tests_errors"]) or 0
            skipped = pick(["skipped", "tests_skipped"]) or 0
            passed = pick(["passed", "passes", "tests_passed"])

            if passed is None and total is not None:
                try:
                    passed = max(total - failed - errors, 0)
                except TypeError:
                    passed = None

            pass_pct = None
            if total and passed is not None:
                try:
                    pass_pct = (passed / total) * 100 if total else None
                except ZeroDivisionError:
                    pass_pct = None

            return {
                "total": cast(total),
                "passed": cast(passed),
                "failed": cast(failed),
                "error": cast(errors),
                "skipped": cast(skipped),
                "pass_pct": pass_pct,
            }

        for raw_line in raw_lines:
            line = raw_line.strip()
            if not line:
                continue
            if len(line) > max_bytes:
                history["ignored_lines"] += 1
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                history["ignored_lines"] += 1
                continue

            event_type = entry.get("event") or "legacy_session"

            # Collect reactor + module-failure evidence from any entry shape so
            # the returned history can surface per-module build status for the
            # submodule metrics assembler.
            reactor_summary = entry.get("reactor_summary")
            if isinstance(reactor_summary, list):
                reactor_records.extend(
                    rec for rec in reactor_summary if isinstance(rec, dict)
                )
            entry_failed_modules = entry.get("failed_modules")
            if isinstance(entry_failed_modules, list):
                failed_modules.extend(str(mod) for mod in entry_failed_modules if mod)

            if event_type == "test_module_summary":
                module_name = entry.get("module") or entry.get("module_name")
                if not module_name:
                    continue
                counts = normalize_tests(entry.get("tests", entry))
                modules_seen[module_name] = {
                    "total": counts["total"],
                    "passed": counts["passed"],
                    "failed": counts["failed"],
                    "error": counts["error"],
                    "skipped": counts["skipped"],
                    "pass_pct": counts["pass_pct"],
                }

                excluded = entry.get("exclusions", {})
                if isinstance(excluded, dict):
                    excluded_tests.update(excluded.get("tests", []) or [])
                    excluded_modules.update(excluded.get("modules", []) or [])
                skipped_modules.update(entry.get("skipped_modules", []) or [])
                failed_tests.update(entry.get("failed_tests", []) or [])
                continue

            if event_type in {"test_session_end", "legacy_session"}:
                aggregate_entry = entry
                counts = normalize_tests(entry.get("tests", entry))
                aggregate_entry["_normalized_tests"] = counts

                modules_expected = entry.get("modules_expected", modules_expected)
                if entry.get("modules_seen"):
                    try:
                        seen = int(entry["modules_seen"])
                        aggregate_entry["_modules_seen"] = seen
                    except (TypeError, ValueError):
                        pass

                skipped_modules.update(entry.get("skipped_modules", []) or [])
                failed_tests.update(entry.get("failed_tests", []) or [])

                exclusions_field = entry.get("exclusions")
                if isinstance(exclusions_field, dict):
                    excluded_tests.update(exclusions_field.get("tests", []) or [])
                    excluded_modules.update(exclusions_field.get("modules", []) or [])
                else:
                    excluded_tests.update(entry.get("excluded_tests", []) or [])
                    excluded_modules.update(entry.get("excluded_modules", []) or [])

                history["last_cmd"] = {
                    "tool": entry.get("tool"),
                    "workdir": entry.get("working_directory"),
                    "exit_code": entry.get("exit_code"),
                    "command": entry.get("command"),
                    "fail_at_end": entry.get("fail_at_end"),
                }

                # Infer fail_at_end from command if not explicit
                if history["last_cmd"].get("fail_at_end") is None:
                    command_str = entry.get("command") or ""
                    history["last_cmd"]["fail_at_end"] = (
                        "--fail-at-end" in command_str or " -fae" in command_str
                    )

        # Keep the history when a build recorded reactor evidence even if no tests
        # ran (build_summary entries) — otherwise the reactor module list would be
        # discarded and the module metrics would fall back to the depth-limited
        # filesystem scan.
        if not modules_seen and not aggregate_entry and not reactor_records and not failed_modules:
            return {}

        aggregate_counts = aggregate_entry.get("_normalized_tests", {}) if aggregate_entry else {}
        aggregate: Dict[str, Any] = {
            "modules_expected": modules_expected,
            "skipped_modules": sorted(skipped_modules) if skipped_modules else [],
            "tests": {
                "total": aggregate_counts.get("total"),
                "passed": aggregate_counts.get("passed"),
                "failed": aggregate_counts.get("failed"),
                "error": aggregate_counts.get("error"),
                "skipped": aggregate_counts.get("skipped"),
            },
            "pass_pct": aggregate_counts.get("pass_pct"),
        }

        modules_seen_count = aggregate_entry.get("_modules_seen") if aggregate_entry else None
        if modules_seen_count is None:
            modules_seen_count = len(modules_seen)
        aggregate["modules_seen"] = modules_seen_count

        # Detect inconsistencies between per-module totals and aggregate
        if aggregate_counts.get("total") is not None and modules_seen:
            module_total = 0.0
            for module_info in modules_seen.values():
                if module_info.get("total") is not None:
                    module_total += module_info["total"]
            try:
                aggregate["inconsistent"] = abs(module_total - aggregate_counts["total"]) > 0.5
            except TypeError:
                aggregate["inconsistent"] = True

        history["aggregate"] = aggregate
        history["per_module"] = modules_seen
        history["exclusions"]["tests"] = sorted(excluded_tests)
        history["exclusions"]["modules"] = sorted(excluded_modules)
        history["failed_tests"] = sorted(failed_tests)
        history["reactor_records"] = reactor_records
        history["failed_modules"] = sorted(dict.fromkeys(failed_modules))
        history["flags"]["fail_at_end"] = (
            history["last_cmd"].get("fail_at_end") if history["last_cmd"] else None
        )

        return history

    def _build_report_snapshot(
        self,
        verified_status: str,
        report_filename: str,
        project_info: dict,
        actual_accomplishments: dict,
        execution_metrics: dict,
        evidence_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a normalized snapshot used for rendering condensed and markdown reports."""

        actual_accomplishments = actual_accomplishments or {}
        execution_metrics = execution_metrics or {}

        test_history = execution_metrics.get("test_history", {}) or {}
        aggregate = test_history.get("aggregate", {}) or {}
        per_module = test_history.get("per_module", {}) or {}

        # Get test count metrics from trunk context if available
        static_test_count = None
        method_count = None
        parameterized_info = {}
        trunk_context = None
        if self.context_manager:
            try:
                trunk_context = self.context_manager.load_trunk_context()
                if trunk_context and trunk_context.environment_summary:
                    # Get the static test method count collected during analysis
                    static_test_count = trunk_context.environment_summary.get("static_test_count")
                    # Get the method count for comparison
                    method_count = trunk_context.environment_summary.get("method_count")
                    # Get parameterized test breakdown
                    parameterized_info = trunk_context.environment_summary.get(
                        "parameterized_info", {}
                    )

                    if static_test_count:
                        logger.info("📊 Retrieved test counts from trunk context:")
                        logger.info(f"   - Declared test methods: {static_test_count}")
                        if method_count and method_count != static_test_count:
                            logger.info(f"   - Method annotations: {method_count}")
            except Exception as e:
                logger.debug(f"Could not retrieve test count: {e}")

        def to_int(value):
            if value is None:
                return None
            try:
                value = float(value)
                if value.is_integer():
                    return int(value)
                return int(value)
            except (TypeError, ValueError):
                return None

        tests_counts = aggregate.get("tests", {}) or {}
        aggregate_total = tests_counts.get("total")
        aggregate_passed = tests_counts.get("passed")
        aggregate_failed = tests_counts.get("failed")
        aggregate_error = tests_counts.get("error")
        aggregate_skipped = tests_counts.get("skipped")
        aggregate_pass_pct = aggregate.get("pass_pct")

        physical_validation = actual_accomplishments.get("physical_validation", {}) or {}
        test_analysis = physical_validation.get("test_analysis", {}) or {}

        # Backfill: when analyze never persisted the static test total to the
        # trunk, fall back to the catalog count from the test scan so the
        # "detected" total never silently disappears from the report/logs (the
        # catalog already counted them — e.g. 100 methods). Persist it back to the
        # trunk (best-effort) so other consumers + validate_project_analysis_status
        # stop reporting it as missing.
        if not static_test_count:
            catalog_count = to_int(test_analysis.get("catalog_test_count"))
            if catalog_count:
                static_test_count = catalog_count
                logger.info(
                    f"📊 Backfilled static test count from catalog scan: {catalog_count}"
                )
                if trunk_context is not None and self.context_manager:
                    try:
                        trunk_context.environment_summary["static_test_count"] = catalog_count
                        self.context_manager._save_trunk_context(trunk_context)
                    except Exception as exc:
                        logger.debug(f"Could not persist backfilled static_test_count: {exc}")

        # Python fallback: there is no Java catalog to backfill from, but
        # validate_test_status already carries the pytest --collect-only
        # denominator (test_stats.discovered / static_test_count). Feed it to
        # the SAME static_test_count the execution-coverage gate below reads so
        # a diagnostic subset re-run can never masquerade as the whole suite
        # (live 2026-07-10 requests run: 8 executed of 635 collected).
        if not static_test_count:
            test_status_pv = physical_validation.get("test_status", {}) or {}
            fallback_discovered = to_int(
                (test_status_pv.get("test_stats") or {}).get("discovered")
            ) or to_int(test_status_pv.get("static_test_count"))
            if fallback_discovered:
                static_test_count = fallback_discovered
                logger.info(
                    "📊 Backfilled static test count from pytest collect-only "
                    f"denominator: {fallback_discovered}"
                )
                if trunk_context is not None and self.context_manager:
                    try:
                        trunk_context.environment_summary["static_test_count"] = (
                            fallback_discovered
                        )
                        self.context_manager._save_trunk_context(trunk_context)
                    except Exception as exc:
                        logger.debug(f"Could not persist backfilled static_test_count: {exc}")

        tests_total = test_analysis.get("total_tests")
        tests_failed = test_analysis.get("failed_tests")
        tests_error = test_analysis.get("error_tests")
        tests_skipped = test_analysis.get("skipped_tests")
        tests_passed = test_analysis.get("passed_tests")
        pass_pct = (
            test_analysis.get("pass_rate")
            or test_analysis.get("pass_pct")
            or test_analysis.get("pass_percentage")
        )

        raw_total_tests = test_analysis.get("raw_total_tests")
        raw_passed_tests = test_analysis.get("raw_passed_tests")
        raw_failed_tests = test_analysis.get("raw_failed_tests")
        raw_error_tests = test_analysis.get("raw_error_tests")
        raw_skipped_tests = test_analysis.get("raw_skipped_tests")
        unique_total_tests = test_analysis.get("unique_tests")
        unique_passed_tests = test_analysis.get("unique_passed_tests")
        unique_failed_tests = test_analysis.get("unique_failed_tests")
        unique_error_tests = test_analysis.get("unique_error_tests")
        unique_skipped_tests = test_analysis.get("unique_skipped_tests")

        if tests_total is None:
            tests_total = aggregate_total
        else:
            if raw_total_tests is None and aggregate_total is not None:
                raw_total_tests = aggregate_total

        if tests_passed is None:
            tests_passed = aggregate_passed
        else:
            if raw_passed_tests is None and aggregate_passed is not None:
                raw_passed_tests = aggregate_passed

        if tests_failed is None:
            tests_failed = aggregate_failed
        else:
            if raw_failed_tests is None and aggregate_failed is not None:
                raw_failed_tests = aggregate_failed

        if tests_error is None:
            tests_error = aggregate_error
        else:
            if raw_error_tests is None and aggregate_error is not None:
                raw_error_tests = aggregate_error

        if tests_skipped is None:
            tests_skipped = aggregate_skipped
        else:
            if raw_skipped_tests is None and aggregate_skipped is not None:
                raw_skipped_tests = aggregate_skipped

        if pass_pct is None:
            pass_pct = aggregate_pass_pct

        if pass_pct is None and tests_total and tests_passed is not None:
            try:
                pass_pct = (tests_passed / tests_total) * 100
            except ZeroDivisionError:
                pass_pct = None

        modules_expected = aggregate.get("modules_expected")
        modules_seen = aggregate.get("modules_seen")
        skipped_modules = aggregate.get("skipped_modules", []) or []

        exclusions = test_history.get("exclusions", {}) or {}
        exclusions_tests = exclusions.get("tests", []) or []
        exclusions_modules = exclusions.get("modules", []) or []

        phases = {
            "clone": actual_accomplishments.get("repository_cloned", False),
            "build": actual_accomplishments.get("build_success", False),
            "test": actual_accomplishments.get("test_success", False),
        }

        # Calculate test metrics with consistent counting. The numerator must
        # share the denominator's basis:
        # - Maven/Gradle: static_test_count counts declared test METHODS
        #   (catalog scan), so execution_rate measures method-level coverage
        #   with the param-stripped unique method count as numerator.
        # - Python: static_test_count comes from pytest --collect-only, which
        #   counts parameterized invocations ([param] EXPANDED). The parser's
        #   executed union preserves [param] precisely so it stays comparable
        #   to the collect-only count — using the stripped unique method count
        #   here read a full 100%-green run (50 collected, 50 executed, 21
        #   methods) as 42% coverage and falsely fired tests_not_fully_executed.
        execution_rate = None
        expansion_factor = None

        executed_tests = to_int(tests_total)
        raw_executed_tests = to_int(raw_total_tests)
        unique_executed_tests = to_int(unique_total_tests)
        if static_test_count and self._is_python_project(project_info):
            coverage_executed_tests = (
                executed_tests if executed_tests is not None else unique_executed_tests
            )
        else:
            coverage_executed_tests = unique_executed_tests or executed_tests

        if static_test_count and coverage_executed_tests is not None:
            try:
                execution_rate = (coverage_executed_tests / static_test_count) * 100
                logger.info(
                    f"📊 Test Coverage: {execution_rate:.1f}% ({coverage_executed_tests} of {static_test_count} tests executed)"
                )
            except (TypeError, ZeroDivisionError):
                execution_rate = None

        if (
            raw_executed_tests
            and unique_executed_tests
            and raw_executed_tests > unique_executed_tests
        ):
            try:
                expansion_factor = raw_executed_tests / unique_executed_tests
                logger.info(
                    f"📊 Parameterized expansion detected: {raw_executed_tests} raw runs vs {unique_executed_tests} unique methods"
                )
                logger.info(f"   - Average expansions per test: {expansion_factor:.2f}x")
            except ZeroDivisionError:
                expansion_factor = None
        elif method_count and executed_tests is not None and method_count:
            try:
                # Provide coverage relative to declared methods when deduplicated counts are available
                method_coverage = (executed_tests / method_count) * 100
                logger.info(
                    f"📊 Method coverage: {method_coverage:.1f}% ({executed_tests} of {method_count} methods)"
                )
            except ZeroDivisionError:
                pass

        status = {
            "overall": verified_status,
            "tests_total": to_int(tests_total),
            "tests_total_raw": to_int(raw_total_tests),
            "tests_passed": to_int(tests_passed),
            "tests_passed_raw": to_int(raw_passed_tests),
            "tests_failed": to_int(tests_failed),
            "tests_failed_raw": to_int(raw_failed_tests),
            "tests_errors": to_int(tests_error),
            "tests_errors_raw": to_int(raw_error_tests),
            "tests_skipped": to_int(tests_skipped),
            "tests_skipped_raw": to_int(raw_skipped_tests),
            "tests_unique": to_int(unique_total_tests),
            "tests_passed_unique": to_int(unique_passed_tests),
            "tests_failed_unique": to_int(unique_failed_tests),
            "tests_errors_unique": to_int(unique_error_tests),
            "tests_skipped_unique": to_int(unique_skipped_tests),
            "pass_pct": pass_pct,
            "static_test_count": static_test_count,
            "method_count": method_count,  # Original method annotations
            "execution_rate": execution_rate,
            "expansion_factor": expansion_factor,
            "parameterized_info": parameterized_info,
            "modules_expected": to_int(modules_expected),
            "modules_seen": to_int(modules_seen),
            "skipped_modules": skipped_modules,
        }

        if (
            status["tests_passed"] is None
            and status["tests_total"] is not None
            and status["tests_failed"] is not None
        ):
            try:
                status["tests_passed"] = max(
                    status["tests_total"] - status["tests_failed"] - (status["tests_errors"] or 0),
                    0,
                )
            except TypeError:
                status["tests_passed"] = None

        tests_ok = None
        if pass_pct is not None:
            # Use the SAME pass-rate threshold as the run verdict so the
            # dashboard pass/fail can never diverge from the header verdict.
            snapshot_threshold_pct = (
                getattr(
                    self.physical_validator,
                    "test_pass_threshold",
                    DEFAULT_TEST_PASS_THRESHOLD,
                )
                * 100.0
            )
            tests_ok = pass_pct >= snapshot_threshold_pct
        elif status["tests_total"] is not None:
            tests_ok = actual_accomplishments.get("test_success", False)
        status["tests_ok"] = tests_ok

        # Carry the build validator's detected system + fingerprint evidence
        # into the snapshot so the condensed summary can render build evidence
        # appropriate to the ecosystem: on python the fingerprint_details ARE
        # the build evidence (venv/pip check/imports/compileall ladder) and the
        # Java "0 .class, 0 .jar" line is suppressed.
        build_evidence = (physical_validation.get("build_status") or {}).get(
            "evidence"
        ) or {}
        physical_evidence = {
            "class_files": physical_validation.get("class_files"),
            "jar_files": physical_validation.get("jar_files"),
            "tests_total": status["tests_total"],
            "tests_pass_pct": pass_pct,
            "build_system": build_evidence.get("build_system"),
            "fingerprint_details": build_evidence.get("fingerprint_details") or {},
        }

        flags = {
            "fail_at_end": test_history.get("flags", {}).get("fail_at_end"),
            "excluded_tests": exclusions_tests,
            "excluded_modules": exclusions_modules,
        }

        snapshot = {
            "status": status,
            "project": {
                "type": project_info.get("type", "Unknown"),
                "build_system": project_info.get("build_system", "Unknown"),
            },
            "phases": phases,
            "report_path": f"/workspace/{report_filename}",
            "physical_evidence": physical_evidence,
            "test_history": test_history,
            "per_module": per_module,
            "flags": flags,
            "last_command": test_history.get("last_cmd", {}),
            "failed_tests": test_history.get("failed_tests", []),
            "evidence_result": evidence_result or {},
        }

        # Module-coverage shortfall caps the run at PARTIAL too: a reactor that
        # built fewer modules than it attempted is not a full success even when
        # the physical class-coverage check passed. Surface it as the SAME
        # conflict the build validator emits (build_modules_incomplete) so the
        # verdict kernel caps it (the conflict is not in ADJUDICATED_CONFLICTS).
        if (
            status.get("modules_expected")
            and status.get("modules_seen") is not None
            and status["modules_seen"] < status["modules_expected"]
        ):
            ev_conflicts = snapshot["evidence_result"].setdefault("conflicts", [])
            if "build_modules_incomplete" not in ev_conflicts:
                ev_conflicts.append("build_modules_incomplete")

        # Test-execution shortfall caps the run at PARTIAL: a static suite was
        # detected but only a fraction actually ran (e.g. carbondata 1/1122 = 0.1%).
        # Mirror the build-coverage gate — emit tests_not_fully_executed (a genuine,
        # non-adjudicated conflict) when execution coverage is below the configured
        # threshold, so the verdict kernel caps an otherwise-clean run at partial.
        exec_threshold_pct = (
            getattr(
                self.physical_validator,
                "test_execution_threshold",
                DEFAULT_TEST_EXECUTION_THRESHOLD,
            )
            * 100.0
        )
        if (
            status.get("static_test_count")
            and status.get("execution_rate") is not None
            and status["execution_rate"] < exec_threshold_pct
        ):
            ev_conflicts = snapshot["evidence_result"].setdefault("conflicts", [])
            if "tests_not_fully_executed" not in ev_conflicts:
                ev_conflicts.append("tests_not_fully_executed")

        # Scope shortfall caps the run at PARTIAL: tests ran in a strict subset
        # of the test-bearing modules (leaf-scoped run in a reactor). Same
        # non-adjudicated conflict mechanism as the two gates above (spec §4).
        # The module counts are folded into status HERE — not only in the
        # caller's dashboard passthrough, which runs after this snapshot is
        # built — because the stored verdict below must already see the cap.
        # _build_module_metrics is memoized on self, so the caller's later
        # passthrough reads the identical cached result (no extra scans).
        try:
            module_metrics = self._build_module_metrics(
                test_history or self._load_test_history() or {},
                generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"module metrics for scope-narrowing check skipped: {exc}")
            module_metrics = None
        if module_metrics:
            msum = module_metrics.get("module_summary") or {}
            status["modules_tested"] = msum.get("modules_tested")
            status["modules_test_bearing"] = msum.get("modules_test_bearing")
        if (
            status.get("modules_test_bearing")
            and status.get("modules_tested") is not None
            and 0 < status["modules_tested"] < status["modules_test_bearing"]
        ):
            ev_conflicts = snapshot["evidence_result"].setdefault("conflicts", [])
            if "reactor_scope_narrowed" not in ev_conflicts:
                ev_conflicts.append("reactor_scope_narrowed")

        # Stored verdict comes from the SAME kernel inputs as the header's
        # Result line (spec §6): header-vs-stored divergence is impossible.
        status["verdict"] = self._snapshot_kernel_verdict(snapshot)

        attention = self._evaluate_attention_flags(snapshot)
        snapshot["attention"] = {
            "items": [f"{item['icon']} {item['message']}" for item in attention],
            "raw": attention,
            "ignored_lines": test_history.get("ignored_lines", 0),
        }

        return snapshot

    def _evaluate_attention_flags(self, snapshot: Dict[str, Any]) -> List[Dict[str, str]]:
        """Evaluate needs-attention rules and return ordered severity entries."""

        severity_order = {"BLOCKER": 0, "WARNING": 1, "INFO": 2}
        severity_icons = {"BLOCKER": "🔴", "WARNING": "🟠", "INFO": "🔵"}
        items: List[Dict[str, str]] = []

        phases = snapshot.get("phases", {})
        status = snapshot.get("status", {})
        test_history = snapshot.get("test_history", {})
        per_module = snapshot.get("per_module", {})
        flags = snapshot.get("flags", {})

        def add(severity: str, message: str):
            items.append(
                {"severity": severity, "icon": severity_icons[severity], "message": message}
            )

        # BLOCKER: build failure
        if not phases.get("build", False):
            add("BLOCKER", "Build failed - compilation or packaging incomplete.")

        # BLOCKER: tests flagged unsuccessful despite telemetry
        if status.get("tests_total") and phases.get("test") is False:
            pass_rate = format_percentage(status.get("pass_pct"))
            add("BLOCKER", f"Tests reported failures (pass rate {pass_rate}).")

        # BLOCKER: build succeeded but no test telemetry captured
        if phases.get("build") and not status.get("tests_total"):
            add("BLOCKER", "No test reports detected despite successful build.")

        # WARNING: pass rate below threshold (unless already blocker)
        if status.get("pass_pct") is not None and status["pass_pct"] < 80:
            pass_rate = format_percentage(status["pass_pct"])
            add("WARNING", f"Test pass rate below threshold (80%): {pass_rate}.")

        # WARNING: module coverage shortfall
        if status.get("modules_expected") and status.get("modules_seen") is not None:
            if status["modules_seen"] < status["modules_expected"]:
                add(
                    "WARNING",
                    f"Module coverage incomplete ({status['modules_seen']}/{status['modules_expected']} tested).",
                )

        # WARNING: skipped modules or exclusions present
        skipped_modules = status.get("skipped_modules") or []
        if skipped_modules:
            skipped_str = truncate_list(skipped_modules)
            add("WARNING", f"Skipped modules detected: {skipped_str}.")

        exclusions_tests = flags.get("excluded_tests") or []
        if exclusions_tests:
            exclusion_str = truncate_list(exclusions_tests)
            add("WARNING", f"Excluded tests patterns applied: {exclusion_str}.")

        # INFO: fail_at_end flag
        if flags.get("fail_at_end"):
            add("INFO", "fail_at_end enabled (test failures may be deferred).")

        # INFO: modules with low pass percentage
        low_modules = []
        for module, data in per_module.items():
            module_pass = data.get("pass_pct")
            if module_pass is not None and module_pass < 80:
                low_modules.append(f"{module} ({format_percentage(module_pass)})")

        if low_modules:
            low_modules.sort(key=lambda entry: entry)
            add("INFO", f"Modules below 80% pass rate: {truncate_list(low_modules)}.")

        # INFO: ignored telemetry lines
        ignored_lines = test_history.get("ignored_lines", 0)
        if ignored_lines:
            add("INFO", f"Telemetry entries ignored during aggregation: {ignored_lines}.")

        items.sort(key=lambda entry: severity_order[entry["severity"]])
        return items

    def _generate_condensed_log_output(
        self,
        verified_status: str,
        report_filename: str,
        actual_accomplishments: dict = None,
        report_snapshot: dict = None,
    ) -> str:
        """Generate condensed output for logs using the shared rendering utility.

        Renders strictly from the validated report snapshot (spec §4: no
        independent stat computation in the report tool). The banner and the
        Next line consume the kernel verdict (spec §6) — never verified_status
        or status.overall, which can sit above it (round-6 review: the
        condensed log announced '✅ SUCCESS ... 🎉' while the report header
        for the SAME snapshot said '**Result:** ⚠️ PARTIAL')."""
        snapshot = dict(report_snapshot or {})
        snapshot["report_path"] = snapshot.get("report_path") or f"/workspace/{report_filename}"

        status = dict(snapshot.get("status") or {})
        kernel_verdict = status.get("verdict") or self._snapshot_kernel_verdict(snapshot)
        status["verdict"] = kernel_verdict
        snapshot["status"] = status

        condensed_lines = render_condensed_summary(snapshot).split("\n")

        if not actual_accomplishments and not self.physical_validator:
            condensed_lines.append(
                "[⚠️ WARNING: No physical validator - using task-based inference only]"
            )

        if kernel_verdict == "success":
            condensed_lines.append("💡 Next: Project ready for development/deployment! 🎉")
        elif kernel_verdict == "failed":
            condensed_lines.append("💡 Next: Review logs and fix build/test failures")
        else:
            condensed_lines.append("💡 Next: Check error logs and retry setup")

        return "\n".join(condensed_lines)

    def _validate_context_prerequisites(self) -> Dict[str, Any]:
        """
        Check context availability for report generation.

        IMPORTANT: TODO list completion is NOT a prerequisite for report generation.
        The final status (success/fail) is determined solely by:
        - Build validation: Must pass
        - Test pass rate: Must be > 80%

        TODO list is tracked for visibility but does not affect the final status.
        """
        logger.info("Starting prerequisite validation for report generation")

        if not self.context_manager:
            # If no context manager available, allow report generation (backward compatibility)
            logger.warning("No context manager available for prerequisite validation")
            return {"valid": True}

        try:
            # Load trunk context to check task statuses with timeout protection
            logger.info("Loading trunk context for validation")
            trunk_context = self.context_manager.load_trunk_context()
            if not trunk_context:
                return {
                    "valid": False,
                    "error": "Cannot generate report: No project plan found",
                    "suggestions": [
                        "Ensure the project has been properly initialized",
                        "Use manage_context to check current project state",
                    ],
                }

            # Check each task status - CRITICAL: Exclude reporting tasks to avoid logical deadlock
            logger.info(f"Checking {len(trunk_context.todo_list)} tasks for completion status")
            incomplete_tasks = []
            for task in trunk_context.todo_list:
                logger.debug(
                    f"Checking task {task.id}: {task.description} - Status: {task.status.value}"
                )
                if task.status.value != "completed":
                    # CRITICAL FIX: Allow reporting task to be in_progress when calling report tool
                    # This prevents the chicken-and-egg problem where the report tool can't run
                    # until the "generate report" task is complete, but the task can't be completed
                    # without running the report tool.
                    if self._is_reporting_task(task):
                        logger.info(
                            f"✅ Allowing reporting task {task.id} to be in_progress during report generation"
                        )
                        continue  # Skip reporting tasks from the prerequisite check

                    logger.debug(f"Task {task.id} is incomplete: {task.status.value}")
                    incomplete_tasks.append(
                        {
                            "id": task.id,
                            "description": task.description,
                            "status": task.status.value,
                        }
                    )

            # Log TODO status but don't block report generation
            # Status is determined by build/test results, not TODO completion
            if incomplete_tasks:
                total_tasks = len(trunk_context.todo_list)
                completed_tasks = total_tasks - len(incomplete_tasks)
                completion_percentage = (
                    (completed_tasks / total_tasks) * 100 if total_tasks > 0 else 0
                )

                logger.info(
                    f"📊 TODO List Status: {completed_tasks}/{total_tasks} tasks complete ({completion_percentage:.1f}%)"
                )
                logger.info(
                    f"📝 {len(incomplete_tasks)} tasks remain incomplete (not blocking report)"
                )

                # Log incomplete tasks for visibility
                for task in incomplete_tasks:
                    logger.debug(
                        f"  • Incomplete: {task['id']}: {task['description']} (status: {task['status']})"
                    )
            else:
                logger.info("✅ All TODO tasks completed")

            # Always allow report generation - status based on build/test results only
            return {"valid": True}

        except Exception as e:
            logger.error(f"Failed to validate context prerequisites: {e}")
            # In case of error, allow report generation but log the issue
            return {"valid": True}

    def _is_reporting_task(self, task) -> bool:
        """
        Determine if a task is related to report generation.
        This prevents logical deadlock where report tool can't run until reporting task is complete.
        """
        reporting_keywords = [
            "report",
            "completion",
            "summary",
            "generate",
            "final",
            "document",
            "conclude",
            "finish",
            "wrap",
        ]

        task_description = task.description.lower()
        return any(keyword in task_description for keyword in reporting_keywords)

    def _reconcile_status(
        self, claimed_status: str, evidence_status: str, accomplishments: dict
    ) -> str:
        """
        Reconcile claimed status with evidence-based status.

        Uses the SINGLE verdict policy (``evaluate_run_verdict``) shared with
        ``_determine_actual_status`` and the physical validator so this fallback
        path can never diverge from the primary verdict:
        build green AND pass_rate >= ``test_pass_threshold`` -> success; else fail.
        A build-green run that passed the threshold is a SUCCESS (partial pass),
        not a failure.
        """
        # Extract core step results
        repository_cloned = accomplishments.get("repository_cloned", False)
        build_success = accomplishments.get("build_success", False)

        # Calculate test pass rate if available
        test_pass_rate = 0.0
        if (
            "physical_validation" in accomplishments
            and "test_analysis" in accomplishments["physical_validation"]
        ):
            test_data = accomplishments["physical_validation"]["test_analysis"]
            # Use PhysicalValidator's method if available
            if self.physical_validator:
                test_pass_rate = self.physical_validator.calculate_test_pass_rate(test_data)
            else:
                total_tests = test_data.get("total_tests", 0)
                passed_tests = test_data.get("passed_tests", 0)
                if total_tests > 0:
                    test_pass_rate = (passed_tests / total_tests) * 100
        elif accomplishments.get("test_success", False):
            # If tests marked as success without detailed data, assume high pass rate
            test_pass_rate = 100.0

        logger.info(
            f"🔍 Status reconciliation - Claimed: '{claimed_status}', Evidence: '{evidence_status}'"
        )
        logger.info(
            f"📊 Core steps - Clone: {repository_cloned}, Build: {build_success}, Test pass rate: {test_pass_rate:.1f}%"
        )

        # Evidence-based status is authoritative
        if not repository_cloned:
            logger.error("❌ Repository clone failed - cannot proceed")
            return "fail"

        if not build_success:
            logger.error("❌ Build failed - compilation issues prevent success")
            return "fail"

        # Delegate the test gate to the single verdict policy so this fallback
        # path agrees with _determine_actual_status (no header-vs-dashboard drift).
        threshold = getattr(
            self.physical_validator, "test_pass_threshold", DEFAULT_TEST_PASS_THRESHOLD
        )
        threshold_pct = threshold * 100.0
        verdict = evaluate_run_verdict(
            build_green=True, pass_rate=test_pass_rate, test_pass_threshold=threshold
        )
        if verdict == "success":
            logger.info(
                f"✅ Success confirmed: Build passed, Test pass rate "
                f"{test_pass_rate:.1f}% >= {threshold_pct:.0f}%"
            )
            return "success"
        logger.warning(f"❌ Fail: Test pass rate {test_pass_rate:.1f}% < {threshold_pct:.0f}%")
        return "fail"

    def _collect_execution_metrics(self) -> dict:
        """Collect comprehensive execution metrics from the session."""
        metrics = {
            "total_runtime": 0,
            "start_time": None,
            "end_time": None,
            "model": None,
            "max_iterations": None,
            "total_steps": 0,
            "total_iterations": 0,
            "total_thoughts": 0,
            "total_actions": 0,
            "total_observations": 0,
            "successful_actions": 0,
            "failed_actions": 0,
            "success_rate": 0,
            "tools_used": {},
            "tool_failures": {},
            "thinking_model_calls": 0,
            "action_model_calls": 0,
            "error_types": {},
            "repetitive_failures": 0,
        }

        # Get execution history if available
        if self.execution_history_callback:
            try:
                history_payload = self.execution_history_callback()

                summary_snapshot = {}
                history_steps: List[Any] = []

                if isinstance(history_payload, dict):
                    history_steps = list(history_payload.get("steps") or [])
                    summary_snapshot = history_payload.get("summary") or {}

                    # Prefer direct iteration count when provided (aligns with agent summary)
                    if summary_snapshot.get("iterations") is not None:
                        metrics["total_iterations"] = summary_snapshot["iterations"]
                    elif history_payload.get("current_iteration") is not None:
                        metrics["total_iterations"] = history_payload["current_iteration"]

                    # Runtime metadata the web read model surfaces (model/step budget).
                    if summary_snapshot.get("model") is not None:
                        metrics["model"] = summary_snapshot["model"]
                    if summary_snapshot.get("max_iterations") is not None:
                        metrics["max_iterations"] = summary_snapshot["max_iterations"]
                else:
                    history_steps = list(history_payload or [])
                    summary_snapshot = {}

                if history_steps:
                    # Calculate timing
                    first_step = history_steps[0]
                    last_step = history_steps[-1]

                    # Get timestamps (handle both object and dict formats)
                    if hasattr(first_step, "timestamp"):
                        metrics["start_time"] = first_step.timestamp
                    elif isinstance(first_step, dict):
                        metrics["start_time"] = first_step.get("timestamp")

                    if hasattr(last_step, "timestamp"):
                        metrics["end_time"] = last_step.timestamp
                    elif isinstance(last_step, dict):
                        metrics["end_time"] = last_step.get("timestamp")

                    # Calculate runtime if we have timestamps
                    if metrics["start_time"] and metrics["end_time"]:
                        from datetime import datetime

                        try:
                            start = datetime.fromisoformat(
                                metrics["start_time"].replace("Z", "+00:00")
                            )
                            end = datetime.fromisoformat(metrics["end_time"].replace("Z", "+00:00"))
                            metrics["total_runtime"] = (
                                end - start
                            ).total_seconds() / 60  # in minutes
                        except:
                            pass

                    # Count step types
                    for step in history_steps:
                        # Handle both object and dict formats
                        if hasattr(step, "step_type"):
                            step_type = step.step_type
                            tool_name = step.tool_name
                            tool_result = step.tool_result
                            model_used = step.model_used
                        elif isinstance(step, dict):
                            step_type = step.get("step_type")
                            tool_name = step.get("tool_name")
                            tool_result = step.get("tool_result")
                            model_used = step.get("model_used")
                        else:
                            continue

                        # Count by type (model-call totals come from the engine
                        # summary override below — the single source of truth)
                        if step_type == "thought":
                            metrics["total_thoughts"] += 1
                        elif step_type == "action":
                            metrics["total_actions"] += 1

                            # Track tool usage
                            if tool_name:
                                metrics["tools_used"][tool_name] = (
                                    metrics["tools_used"].get(tool_name, 0) + 1
                                )

                                # Check success/failure
                                success = False
                                if hasattr(tool_result, "success"):
                                    success = tool_result.success
                                elif isinstance(tool_result, dict):
                                    success = tool_result.get("success", False)

                                if success:
                                    metrics["successful_actions"] += 1
                                else:
                                    metrics["failed_actions"] += 1
                                    metrics["tool_failures"][tool_name] = (
                                        metrics["tool_failures"].get(tool_name, 0) + 1
                                    )

                                    # Track error types
                                    error_code = None
                                    if hasattr(tool_result, "error_code"):
                                        error_code = tool_result.error_code
                                    elif isinstance(tool_result, dict):
                                        error_code = tool_result.get("error_code")

                                    if error_code:
                                        metrics["error_types"][error_code] = (
                                            metrics["error_types"].get(error_code, 0) + 1
                                        )
                                        if error_code == "REPETITIVE_EXECUTION":
                                            metrics["repetitive_failures"] += 1

                        elif step_type == "observation":
                            metrics["total_observations"] += 1

                    # Calculate success rate
                    if metrics["total_actions"] > 0:
                        metrics["success_rate"] = (
                            metrics["successful_actions"] / metrics["total_actions"]
                        ) * 100

                # Override with authoritative summary data when available
                if summary_snapshot:
                    metrics["total_steps"] = summary_snapshot.get(
                        "total_steps", metrics["total_steps"]
                    )
                    metrics["total_thoughts"] = summary_snapshot.get(
                        "thoughts", metrics["total_thoughts"]
                    )
                    metrics["total_actions"] = summary_snapshot.get(
                        "actions", metrics["total_actions"]
                    )
                    metrics["total_observations"] = summary_snapshot.get(
                        "observations", metrics["total_observations"]
                    )
                    metrics["successful_actions"] = summary_snapshot.get(
                        "successful_actions", metrics["successful_actions"]
                    )
                    metrics["failed_actions"] = summary_snapshot.get(
                        "failed_actions", metrics["failed_actions"]
                    )
                    metrics["thinking_model_calls"] = summary_snapshot.get(
                        "thinking_model_calls", metrics["thinking_model_calls"]
                    )
                    metrics["action_model_calls"] = summary_snapshot.get(
                        "action_model_calls", metrics["action_model_calls"]
                    )

                    # Per-tool usage from the summary is the whole-run truth.
                    # The per-step loop above only saw the live (post-compaction)
                    # window, so in phase mode it holds just the report phase.
                    if summary_snapshot.get("tools_used"):
                        metrics["tools_used"] = dict(summary_snapshot["tools_used"])
                    if summary_snapshot.get("tool_failures") is not None:
                        metrics["tool_failures"] = dict(summary_snapshot["tool_failures"])

                    if summary_snapshot.get("actions"):
                        try:
                            metrics["success_rate"] = (
                                summary_snapshot.get("successful_actions", 0)
                                / summary_snapshot["actions"]
                            ) * 100
                        except ZeroDivisionError:
                            metrics["success_rate"] = 0

            except Exception as e:
                logger.warning(f"Failed to collect execution metrics: {e}")

        test_history = self._load_test_history()
        if test_history:
            metrics["test_history"] = test_history

        return metrics

    def _verify_execution_history(
        self, claimed_status: str, claimed_summary: str
    ) -> tuple[str, dict]:
        """Verify the claimed status using physical validation instead of inference."""
        # Initialize accomplishments
        actual_accomplishments = {
            "repository_cloned": False,
            "build_success": False,
            "test_success": False,
            "total_actions": 0,
            "successful_actions": 0,
            "physical_validation": {},  # Store physical validation results
        }

        # CRITICAL: Use physical evidence to determine true status
        # Check actual files instead of inferring from logs or task descriptions
        if self.physical_validator and self.docker_orchestrator:
            try:
                project_info = self._get_project_info()
                project_dir = project_info.get("directory", "/workspace")

                # Derive project_name for validator (expects name, not full path)
                project_name_for_validator = None
                try:
                    if project_dir.startswith("/workspace/"):
                        tail = project_dir[len("/workspace/") :].strip("/")
                        # Only use single-segment project name (avoid nested paths)
                        if tail and "/" not in tail:
                            project_name_for_validator = tail
                except Exception:
                    project_name_for_validator = None

                # Ensure physical_validation container exists
                if "physical_validation" not in actual_accomplishments:
                    actual_accomplishments["physical_validation"] = {}

                # Primary build verdict must match agent-facing validation
                build_status = self.physical_validator.validate_build_status(
                    project_name_for_validator
                )
                actual_accomplishments["build_success"] = build_status.get("success", False)
                actual_accomplishments["physical_validation"]["build_status"] = build_status
                logger.info(
                    f"🔍 Build status verification:"
                    f" {'SUCCESS' if actual_accomplishments['build_success'] else 'FAILED'}"
                    f" - {build_status.get('reason', 'no reason provided')}"
                )

                # Use artifact scan for counts/evidence without overriding build_success
                validation_result = self.physical_validator.validate_build_artifacts(
                    project_name=project_name_for_validator
                )

                # Use catalog-aware parsing: besides the runner XML metrics it
                # carries catalog_test_count (the statically discovered test
                # total), which backfills the report's "detected" count when the
                # analyze step failed to persist static_test_count to the trunk.
                test_analysis = self.physical_validator.parse_test_reports_with_catalog(project_dir)

                # Also get test validation status for additional insights
                test_status = self.physical_validator.validate_test_status(
                    project_name_for_validator
                )
                actual_accomplishments["physical_validation"]["test_status"] = test_status

                # Log test status insights
                if test_status.get("pass_rate", 0) <= 80 and test_status.get("has_test_reports"):
                    logger.warning(
                        f"⚠️ Test pass rate is {test_status['pass_rate']:.1f}% (below 80% threshold)"
                    )
                if test_status.get("test_exclusions"):
                    logger.warning(
                        f"⚠️ Detected test exclusions: {', '.join(test_status['test_exclusions'])}"
                    )

                # Extract results from PhysicalValidator
                # Repository is cloned if artifacts detected OR the project directory exists under /workspace
                actual_accomplishments["repository_cloned"] = (
                    validation_result.get("class_files", 0) > 0
                    or validation_result.get("jar_files", 0) > 0
                    or len(validation_result.get("missing_classes", [])) > 0
                    or actual_accomplishments["build_success"]
                )
                # Strengthen with directory existence check
                try:
                    dir_check = self.docker_orchestrator.execute_command(
                        f"test -d {project_dir} && echo EXISTS || echo MISSING"
                    )
                    if "EXISTS" in (dir_check.get("output") or ""):
                        actual_accomplishments["repository_cloned"] = True
                except Exception as _e:
                    logger.debug(f"Directory existence check failed: {_e}")

                # Always store the projected test_analysis — even when no test
                # reports exist (valid=False) it still carries catalog_test_count,
                # the statically discovered test total. Without this, a failed/
                # no-report build discards the "N detected tests" count entirely
                # (the count the user relies on), and the report falls back to a
                # bare "no tests executed".
                actual_accomplishments["physical_validation"]["test_analysis"] = (
                    build_stored_test_analysis(test_analysis)
                )

                if test_analysis.get("valid"):
                    actual_accomplishments["test_success"] = test_analysis["test_success"]

                    # Log if tests were excluded
                    if test_analysis.get("test_exclusions"):
                        logger.warning(
                            f"⚠️ Test exclusions detected: {', '.join(test_analysis['test_exclusions'])}"
                        )

                    if test_analysis["test_success"]:
                        logger.info(
                            f"✅ PHYSICAL: Tests passed - {test_analysis['passed_tests']}/{test_analysis['total_tests']} tests successful"
                        )
                    else:
                        logger.warning(
                            f"❌ PHYSICAL: Tests failed - {test_analysis['failed_tests']} failures, {test_analysis['error_tests']} errors out of {test_analysis['total_tests']} total"
                        )
                else:
                    actual_accomplishments["test_success"] = False
                    logger.info("⚠️ PHYSICAL: No test reports found or parsing failed")

                # Store validation results - initialize if not exists
                actual_accomplishments["physical_validation"].update(
                    {
                        "class_files": validation_result.get("class_files", 0),
                        "jar_files": validation_result.get("jar_files", 0),
                        "recent_compilation": validation_result.get("recent_compilation", False),
                        "missing_classes": len(validation_result.get("missing_classes", [])),
                    }
                )

                # CRITICAL: ENFORCE LOGICAL CONSISTENCY
                if not actual_accomplishments["repository_cloned"]:
                    if (
                        actual_accomplishments["build_success"]
                        or actual_accomplishments["test_success"]
                    ):
                        logger.error("🚨 IMPOSSIBLE STATE: Build/test without repository!")
                    actual_accomplishments["build_success"] = False
                    actual_accomplishments["test_success"] = False
                    logger.info("⚠️ CONSISTENCY: No clone → no build/test")
                elif not actual_accomplishments["build_success"]:
                    if actual_accomplishments["test_success"]:
                        logger.error("🚨 IMPOSSIBLE STATE: Test without build!")
                    actual_accomplishments["test_success"] = False
                    logger.info("⚠️ CONSISTENCY: No build → no test")

                logger.info(
                    f"📊 PHYSICAL TRUTH: Clone={actual_accomplishments['repository_cloned']}, "
                    f"Build={actual_accomplishments['build_success']}, "
                    f"Test={actual_accomplishments['test_success']}"
                )

            except Exception as e:
                logger.warning(f"Physical validation error: {e}")

        # CRITICAL FIX: Enforce logical consistency
        # If build failed, tests cannot have succeeded
        if not actual_accomplishments["build_success"]:
            actual_accomplishments["test_success"] = False
            actual_accomplishments["test_status"] = "not_run"
            logger.info(
                "⚠️ Build failed - marking tests as not run (impossible to test without successful build)"
            )

        # Determine actual status based on accomplishments
        actual_status = self._determine_actual_status(actual_accomplishments)

        # Smart status reconciliation
        if actual_status != claimed_status:
            logger.warning(
                f"🔍 Status verification: Claimed '{claimed_status}' but evidence suggests '{actual_status}'"
            )
            logger.info(f"🔍 Actual accomplishments: {actual_accomplishments}")

            # Reconcile the status - prioritize physical evidence
            if actual_accomplishments.get("physical_validation"):
                logger.info("Using physical validation as primary source of truth")
                return actual_status, actual_accomplishments

            reconciled_status = self._reconcile_status(
                claimed_status, actual_status, actual_accomplishments
            )
            logger.info(f"🤝 Status reconciled: Using '{reconciled_status}' as final status")
            return reconciled_status, actual_accomplishments

        return actual_status, actual_accomplishments

    def _determine_actual_status(self, accomplishments: dict) -> str:
        """
        Determine the actual run verdict from build and test results.

        Delegates the final pass/fail decision to ``evaluate_run_verdict`` (the
        single source of truth shared with the physical validator):
        - SUCCESS: build green AND test pass rate >= ``test_pass_threshold``
        - FAIL: repository not cloned, build failed, no test reports, or test
          pass rate < ``test_pass_threshold``
        """
        # Extract the three core indicators
        repository_cloned = accomplishments.get("repository_cloned", False)
        build_success = accomplishments.get("build_success", False)
        test_success = accomplishments.get("test_success", False)

        logger.debug(
            f"🔍 Core status check - Clone: {repository_cloned}, Build: {build_success}, Test: {test_success}"
        )

        # Step 1: Check if repository was cloned
        if not repository_cloned:
            logger.error("❌ Repository clone failed - this is a fundamental failure")
            return "fail"

        # Step 2: Check if build completed successfully
        if not build_success:
            logger.error("❌ Build failed - cannot proceed without successful compilation")
            return "fail"

        # Step 3: Calculate test pass rate for final determination
        test_pass_rate = 0.0

        # Check if we have physical validation test data
        if (
            "physical_validation" in accomplishments
            and "test_analysis" in accomplishments["physical_validation"]
        ):
            test_data = accomplishments["physical_validation"]["test_analysis"]

            # Use PhysicalValidator's method if available for consistency
            if self.physical_validator:
                test_pass_rate = self.physical_validator.calculate_test_pass_rate(test_data)
            else:
                # Fallback to manual calculation
                total_tests = test_data.get("total_tests", 0)
                passed_tests = test_data.get("passed_tests", 0)
                if total_tests > 0:
                    test_pass_rate = (passed_tests / total_tests) * 100

            if test_pass_rate == 0 and test_data.get("total_tests", 0) == 0:
                logger.warning("⚠️ No test reports found - treating as 0% pass rate")
                return "fail"
            else:
                logger.info(
                    f"📊 Test pass rate: {test_pass_rate:.1f}% ({test_data.get('passed_tests', 0)}/{test_data.get('total_tests', 0)})"
                )
        elif test_success:
            # Assume high pass rate if tests succeeded without detailed data
            test_pass_rate = 100.0
            logger.info("✅ Tests marked as successful (assuming 100% pass rate)")
        else:
            logger.warning("⚠️ No test execution detected - treating as 0% pass rate")
            return "fail"

        # Final determination via the SINGLE verdict policy shared with the
        # physical validator (evaluate_run_verdict) - no hardcoded threshold.
        # At this point the build is green (we returned "fail" above otherwise).
        threshold = getattr(
            self.physical_validator, "test_pass_threshold", DEFAULT_TEST_PASS_THRESHOLD
        )
        threshold_pct = threshold * 100.0
        verdict = evaluate_run_verdict(
            build_green=True, pass_rate=test_pass_rate, test_pass_threshold=threshold
        )
        if verdict == "success":
            logger.info(
                f"✅ SUCCESS: Build passed ✓, Test pass rate "
                f"{test_pass_rate:.1f}% >= {threshold_pct:.0f}% ✓"
            )
            return "success"
        logger.warning(f"❌ FAIL: Test pass rate {test_pass_rate:.1f}% < {threshold_pct:.0f}%")
        return "fail"

    def _generate_console_report(
        self,
        summary: str,
        status: str,
        details: str,
        timestamp: str,
        project_info: dict,
        actual_accomplishments: dict = None,
        execution_metrics: dict = None,
        report_snapshot: dict = None,
    ) -> str:
        """Generate console-formatted report rendered from the validated snapshot."""

        report_lines = [
            "=" * 80,
            "🎯 DETAILED PROJECT SETUP REPORT",
            "=" * 80,
            f"⏰ Generated: {timestamp}",
            f"📊 Status: {status.upper()}",
        ]
        evidence_lines = self._render_console_evidence_result(report_snapshot)
        if evidence_lines:
            report_lines.extend(evidence_lines)
        report_lines.append("")

        # Add project information
        if project_info:
            report_lines.extend(
                [
                    "📂 PROJECT INFORMATION:",
                    f"   • Project Directory: {project_info.get('directory', 'Unknown')}",
                    f"   • Project Type: {project_info.get('type', 'Unknown')}",
                    f"   • Build System: {project_info.get('build_system', 'Unknown')}",
                    "",
                ]
            )

        # Add summary
        if summary:
            report_lines.extend(
                [
                    "📋 SUMMARY:",
                    f"   {summary}",
                    "",
                ]
            )

        # CRITICAL FIX: Use actual TODO list from trunk context instead of hardcoded tasks
        report_lines.extend(
            [
                "✅ TASK COMPLETION STATUS:",
            ]
        )

        # Try to get actual task status from trunk context first
        todo_list_used = False
        if self.context_manager:
            try:
                trunk_context = self.context_manager.load_trunk_context()
                if trunk_context and trunk_context.todo_list:
                    todo_list_used = True

                    for task in trunk_context.todo_list:
                        if task.status.value == "completed":
                            icon = "✅"
                            status_text = "Completed"
                            if task.key_results:
                                status_text += f" - {task.key_results}"
                        elif task.status.value == "in_progress":
                            icon = "🔄"
                            status_text = "In Progress"
                        elif task.status.value == "failed":
                            icon = "❌"
                            status_text = "Failed"
                        else:
                            icon = "⏳"
                            status_text = "Pending"

                        report_lines.append(f"   • {icon} {task.description} - {status_text}")

            except Exception as e:
                logger.warning(f"Failed to load trunk context for console report: {e}")

        # Fallback when no TODO list is available: render the three core phases
        # from the physically-validated accomplishments.
        if not todo_list_used:
            logger.info("Using physical accomplishments as fallback for task status")

            accomplishments = actual_accomplishments or {}
            for label, key in (
                ("Project repository cloning", "repository_cloned"),
                ("Project compilation", "build_success"),
                ("Test execution", "test_success"),
            ):
                icon = "✅" if accomplishments.get(key) else "❌"
                report_lines.append(f"   • {icon} {label}")

        # Add comprehensive execution metrics
        if execution_metrics:
            report_lines.extend(
                [
                    "",
                    "📊 EXECUTION METRICS:",
                ]
            )

            # Runtime metrics
            if execution_metrics.get("total_runtime"):
                report_lines.append(
                    f"   • Total runtime: {execution_metrics['total_runtime']:.1f} minutes"
                )

            # Iteration metrics
            if execution_metrics.get("total_iterations"):
                report_lines.append(
                    f"   • Iterations used: {execution_metrics['total_iterations']}"
                )

            # Step breakdown
            report_lines.extend(
                [
                    f"   • Total thoughts: {execution_metrics.get('total_thoughts', 0)}",
                    f"   • Total actions: {execution_metrics.get('total_actions', 0)}",
                    f"   • Total observations: {execution_metrics.get('total_observations', 0)}",
                ]
            )

            # Success metrics
            successful = execution_metrics.get("successful_actions", 0)
            failed = execution_metrics.get("failed_actions", 0)
            success_rate = execution_metrics.get("success_rate", 0)
            report_lines.extend(
                [
                    f"   • Successful actions: {successful}",
                    f"   • Failed actions: {failed}",
                    f"   • Success rate: {success_rate:.1f}%",
                ]
            )

            # Model usage
            report_lines.extend(
                [
                    f"   • Thinking model calls: {execution_metrics.get('thinking_model_calls', 0)}",
                    f"   • Action model calls: {execution_metrics.get('action_model_calls', 0)}",
                ]
            )

            # Tool usage
            if execution_metrics.get("tools_used"):
                top_tools = sorted(
                    execution_metrics["tools_used"].items(), key=lambda x: x[1], reverse=True
                )[:5]
                tools_str = ", ".join([f"{tool}({count})" for tool, count in top_tools])
                report_lines.append(f"   • Most used tools: {tools_str}")

            # Error patterns
            if execution_metrics.get("repetitive_failures", 0) > 0:
                report_lines.append(
                    f"   • ⚠️ Repetitive failures: {execution_metrics['repetitive_failures']}"
                )

            if execution_metrics.get("error_types"):
                top_errors = sorted(
                    execution_metrics["error_types"].items(), key=lambda x: x[1], reverse=True
                )[:3]
                for error_type, count in top_errors:
                    report_lines.append(f"   • Error type '{error_type}': {count} occurrences")

        # Add legacy execution statistics if no metrics available but accomplishments exist
        elif actual_accomplishments:
            total = actual_accomplishments.get("total_actions", 0)
            successful = actual_accomplishments.get("successful_actions", 0)
            if total > 0:
                success_rate = (successful / total) * 100
                report_lines.extend(
                    [
                        "",
                        f"📊 EXECUTION STATISTICS:",
                        f"   • Total actions executed: {total}",
                        f"   • Successful actions: {successful}",
                        f"   • Success rate: {success_rate:.1f}%",
                    ]
                )

        report_lines.append("")

        # Add details if provided
        if details:
            report_lines.extend(
                [
                    "📝 DETAILS:",
                    f"   {details}",
                    "",
                ]
            )

        # Add next steps based on status (verified status vocabulary: success/fail)
        if status == "success":
            report_lines.extend(
                [
                    "🚀 PROJECT READY:",
                    "   • The project has been successfully set up and tested",
                    "   • All dependencies are installed and configured",
                    "   • You can now start development or deployment",
                    "",
                ]
            )
        else:
            report_lines.extend(
                [
                    "❌ SETUP ISSUES:",
                    "   • Project setup encountered significant problems",
                    "   • Check error logs and dependency requirements",
                    "   • Manual troubleshooting may be required",
                    "",
                ]
            )

        report_lines.extend(
            [
                "=" * 80,
                "Task completed. Setup agent finished.",
                "=" * 80,
            ]
        )

        return "\n".join(report_lines)

    def _get_project_info(self) -> Dict[str, str]:
        """Get basic project information from the workspace."""
        import re  # FIXED: Move import to top level to avoid scope issues

        info = {}

        try:
            if self.docker_orchestrator:
                # First try to get project info from trunk context
                trunk_context = (
                    self.context_manager.load_trunk_context() if self.context_manager else None
                )

                # FIXED: Try to detect actual project directory from completed tasks
                project_dir = "/workspace"
                if trunk_context and hasattr(trunk_context, "todo_list"):
                    for task in trunk_context.todo_list:
                        # FIXED: Use object attributes instead of dictionary access
                        if task.status.value == "completed" and task.key_results:
                            key_results = task.key_results
                            # Look for project directory in key results
                            if "repo_dir=" in key_results:
                                # Extract repo_dir value
                                match = re.search(r"repo_dir=([^;,.\s]+)", key_results)
                                if match:
                                    project_dir = match.group(1)
                                    break
                            elif "path=/workspace/" in key_results:
                                # Try to extract project path - more specific pattern
                                match = re.search(r"path=(/workspace/[\w.-]+)", key_results)
                                if match:
                                    project_dir = match.group(1)
                                    break
                            elif "Directory=" in key_results or "directory=" in key_results:
                                # Handle 'Directory=/workspace/<name>' style
                                match = re.search(r"[Dd]irectory=([^;,.\s]+)", key_results)
                                if match and match.group(1).startswith("/workspace/"):
                                    project_dir = match.group(1)
                                    break
                            elif "clone_location" in key_results:
                                # Handle dict-like key results: {'clone_location': '/workspace/<name>', ...}
                                match = re.search(
                                    r"clone_location['\"]?\s*[:=]\s*['\"](/workspace/[^'\"\s]+)['\"]",
                                    key_results,
                                )
                                if match:
                                    project_dir = match.group(1)
                                    break

                # Fallback: if still /workspace, try to probe a likely project directory
                if project_dir == "/workspace":
                    try:
                        # IMPORTANT: Exclude setup-report files from being treated as project directories.
                        # One find across all build-file types (incl. Gradle Kotlin DSL
                        # build.gradle.kts / settings.gradle[.kts]); `grep .` makes an empty
                        # result exit non-zero so `||` actually falls through to the
                        # last-resort directory probe. The previous per-type `||` chain
                        # short-circuited on the first `find | head` (which exits 0 even when
                        # empty), so any non-Maven project resolved to /workspace.
                        probe_cmd = (
                            "(find /workspace -maxdepth 2 -type f "
                            "\\( -name pom.xml -o -name build.gradle -o -name build.gradle.kts "
                            "-o -name settings.gradle -o -name settings.gradle.kts -o -name package.json \\) "
                            "! -path '*/setup-report-*' -printf '%h\\n' 2>/dev/null | head -1 | grep .) || "
                            "(find /workspace -mindepth 1 -maxdepth 1 -type d ! -name '.setup_agent' ! -name 'setup-report-*' -printf '%p\\n' 2>/dev/null | head -1)"
                        )
                        result = self.docker_orchestrator.execute_command(probe_cmd)
                        candidate = result.get("output", "").strip().split("\n")[0]
                        # Additional check: ensure the candidate is not a report file
                        if (
                            candidate
                            and candidate.startswith("/workspace/")
                            and "setup-report-" not in candidate
                        ):
                            project_dir = candidate
                    except Exception:
                        pass

                # ENHANCED: First try to get project type from task key_results
                project_type_from_tasks = None
                if trunk_context and hasattr(trunk_context, "todo_list"):
                    for task in trunk_context.todo_list:
                        if task.status.value == "completed" and task.key_results:
                            key_results = task.key_results.lower()
                            if "project_type=maven" in key_results:
                                project_type_from_tasks = ("Maven Java Project", "Maven")
                                break
                            elif "project_type=gradle" in key_results:
                                project_type_from_tasks = ("Gradle Java Project", "Gradle")
                                break
                            elif (
                                "project_type=node" in key_results
                                or "project_type=npm" in key_results
                            ):
                                project_type_from_tasks = ("Node.js Project", "npm/yarn")
                                break
                            elif "project_type=python" in key_results:
                                project_type_from_tasks = ("Python Project", "pip/poetry")
                                break

                # Use project type from tasks if found, otherwise check files
                if project_type_from_tasks:
                    info["type"] = project_type_from_tasks[0]
                    info["build_system"] = project_type_from_tasks[1]
                    info["directory"] = project_dir
                    logger.debug(
                        f"✅ Project type detected from task results: {project_type_from_tasks[0]}"
                    )
                else:
                    # Fallback: Check for common project files in the actual project directory
                    result = self.docker_orchestrator.execute_command(f"ls -la {project_dir}")
                    if result.get("success"):
                        output = result.get("output", "")

                        # Determine project type based on files
                        if "pom.xml" in output:
                            info["type"] = "Maven Java Project"
                            info["build_system"] = "Maven"
                        elif "build.gradle" in output:
                            info["type"] = "Gradle Java Project"
                            info["build_system"] = "Gradle"
                        elif "package.json" in output:
                            info["type"] = "Node.js Project"
                            info["build_system"] = "npm/yarn"
                        elif "requirements.txt" in output or "pyproject.toml" in output:
                            info["type"] = "Python Project"
                            info["build_system"] = "pip/poetry"
                        else:
                            info["type"] = "Generic Project"
                            info["build_system"] = "Unknown"

                        info["directory"] = project_dir

        except Exception as e:
            logger.warning(f"Could not gather project info: {e}")

        return info

    def _generate_markdown_report(
        self,
        summary: str,
        status: str,
        details: str,
        timestamp: str,
        project_info: dict,
        actual_accomplishments: dict = None,
        execution_metrics: dict = None,
        report_snapshot: dict = None,
    ) -> str:
        """Generate markdown-formatted report with improved structure and clarity."""

        # Start with enhanced header
        report_lines = self._render_enhanced_header(
            timestamp, status, project_info, report_snapshot
        )

        if report_snapshot:
            # Add summary dashboard with all key metrics
            dashboard_section = self._render_summary_dashboard(report_snapshot)
            if dashboard_section:
                report_lines.extend(dashboard_section)

            # Add detailed test analysis
            test_analysis_section = self._render_detailed_test_analysis(report_snapshot)
            if test_analysis_section:
                report_lines.extend(test_analysis_section)

            # Add per-submodule build/test breakdown (multi-module projects only)
            try:
                mm = self._build_module_metrics(
                    self._load_test_history() or {},
                    generated_at=timestamp,
                )
                report_lines.extend(self._render_submodule_breakdown(mm or {}))
            except Exception as exc:
                logger.debug(f"submodule breakdown skipped: {exc}")

            # Add issues and recommendations
            issues_section = self._render_issues_recommendations(report_snapshot)
            if issues_section:
                report_lines.extend(issues_section)

        # Task progress section with improved format
        task_progress_section = self._render_task_progress(actual_accomplishments)
        if task_progress_section:
            report_lines.extend(task_progress_section)

        # Execution details section (simplified)
        exec_details_section = self._render_execution_details_simplified(
            report_snapshot, execution_metrics
        )
        if exec_details_section:
            report_lines.extend(exec_details_section)

        # Runtime overlay evidence is reported separately from project metadata.
        env_overlay_section = self._render_runtime_env_overlay_evidence()
        if env_overlay_section:
            report_lines.extend(env_overlay_section)

        # Add error analysis section
        error_section = self._generate_error_reporting_section(actual_accomplishments)
        if error_section:
            report_lines.extend(error_section)

        # Generate next steps based on actual status and context
        next_steps_section = self._generate_next_steps_section(status, actual_accomplishments)
        if next_steps_section:
            report_lines.extend(next_steps_section)

        report_lines.extend(
            [
                "---",
                "",
                "**Task completed. Setup Agent has finished.**",
                "",
                f"*This report was automatically generated by Setup-Agent v{self._get_setup_agent_version()} at {timestamp}*",
            ]
        )

        return "\n".join(report_lines)

    def _render_runtime_env_overlay_evidence(self) -> List[str]:
        """Render runtime-only environment overlay evidence when present."""
        if not self.docker_orchestrator:
            return []

        try:
            overlay = EnvOverlayStore(self.docker_orchestrator).inspect()
        except Exception as exc:
            logger.debug(f"Could not inspect runtime env overlay for report: {exc}")
            return []

        tools = overlay.get("tools", {}) or {}
        warnings = overlay.get("warnings", []) or []
        active_rows = []
        blocked_rows = []
        for tool_name, entry in sorted(tools.items()):
            if not isinstance(entry, dict):
                continue

            active = entry.get("active")
            if active:
                candidate = (entry.get("candidates") or {}).get(active, {}) or {}
                active_rows.append(
                    [
                        tool_name,
                        f"`{active}`",
                        candidate.get("version"),
                        candidate.get("source"),
                    ]
                )

            for blocked in entry.get("blocked", []) or []:
                if not isinstance(blocked, dict):
                    continue
                blocked_rows.append(
                    [
                        tool_name,
                        f"`{blocked.get('executable', '')}`",
                        blocked.get("version"),
                        blocked.get("requirement"),
                        self._truncate_overlay_reason(blocked.get("reason")),
                        blocked.get("source"),
                    ]
                )

        if not active_rows and not blocked_rows and not warnings:
            return []

        lines = [
            "## Runtime Environment Overlay Evidence",
            "",
            "This is runtime command evidence, not project source configuration.",
            "",
        ]

        lines.extend(["### Active Tool Executables", ""])
        if active_rows:
            lines.extend(
                [
                    "| Tool | Executable | Version | Source |",
                    "|------|------------|---------|--------|",
                ]
            )
            for row in active_rows:
                lines.append(self._markdown_table_row(row))
        else:
            lines.append("- No active overlay executables recorded.")
        lines.append("")

        lines.extend(["### Blocked Candidates", ""])
        if blocked_rows:
            lines.extend(
                [
                    "| Tool | Executable | Version | Requirement | Reason | Source |",
                    "|------|------------|---------|-------------|--------|--------|",
                ]
            )
            visible_blocked_rows = blocked_rows[:MAX_RUNTIME_ENV_OVERLAY_BLOCKED_ROWS]
            for row in visible_blocked_rows:
                lines.append(self._markdown_table_row(row))
            omitted_count = len(blocked_rows) - len(visible_blocked_rows)
            if omitted_count > 0:
                lines.append(f"- ... (+{omitted_count} more blocked candidates)")
        else:
            lines.append("- No blocked overlay candidates recorded.")
        lines.append("")

        if warnings:
            lines.extend(["### Overlay Warnings", ""])
            for warning in warnings[:5]:
                lines.append(f"- {warning}")
            if len(warnings) > 5:
                lines.append(f"- ... (+{len(warnings) - 5} more)")
            lines.append("")

        return lines

    @staticmethod
    def _truncate_overlay_reason(value: Any) -> Any:
        if value is None:
            return None

        reason = str(value).replace("\n", " ").strip()
        if len(reason) <= MAX_RUNTIME_ENV_OVERLAY_REASON_CHARS:
            return reason

        return f"{reason[: MAX_RUNTIME_ENV_OVERLAY_REASON_CHARS - 3].rstrip()}..."

    @staticmethod
    def _markdown_table_row(values: Iterable[Any]) -> str:
        cells = []
        for value in values:
            if value is None:
                cell = ""
            else:
                cell = str(value).replace("\n", " ").replace("|", "\\|")
            cells.append(cell)
        return f"| {' | '.join(cells)} |"

    def _get_centralized_error_analysis(self) -> list:
        """Get error analysis from centralized error logger."""
        try:
            from sag.agent.error_logger import ErrorLogger

            # Get error logger instance - ensure we use the correct workspace path
            workspace_path = "/workspace"  # Default workspace path in container
            if (
                hasattr(self, "orchestrator")
                and self.orchestrator
                and hasattr(self.orchestrator, "workspace_path")
            ):
                # If orchestrator has a workspace path, use it
                workspace_path = self.orchestrator.workspace_path
            elif (
                hasattr(self, "docker_orchestrator")
                and self.docker_orchestrator
                and hasattr(self.docker_orchestrator, "workspace_path")
            ):
                # Alternative attribute name for orchestrator
                workspace_path = self.docker_orchestrator.workspace_path
            error_logger = ErrorLogger.get_instance(workspace_path=workspace_path)

            # Get error summary
            summary = error_logger.get_error_summary()

            if summary["total_errors"] == 0:
                return []

            analysis_lines = [
                "### 📊 Centralized Error Summary",
                "",
                f"**Total Errors Logged:** {summary['total_errors']}",
                "",
            ]

            # Error type breakdown
            if summary["error_counts_by_type"]:
                analysis_lines.extend(
                    [
                        "**Error Types:**",
                    ]
                )
                for error_type, count in sorted(
                    summary["error_counts_by_type"].items(), key=lambda x: x[1], reverse=True
                ):
                    percentage = (count / summary["total_errors"]) * 100
                    emoji = {
                        "tool_error": "🔧",
                        "unknown_tool": "❓",
                        "validation_error": "✅",
                        "execution_error": "⚡",
                        "system_error": "💥",
                        "timeout_error": "⏰",
                        "recovery_failed": "🔄",
                    }.get(error_type, "⚠️")
                    analysis_lines.append(
                        f"- {emoji} **{error_type}**: {count} ({percentage:.1f}%)"
                    )
                analysis_lines.append("")

            # Tool error categories
            if summary["tool_error_categories"]:
                analysis_lines.extend(
                    [
                        "**Tool Error Categories:**",
                    ]
                )
                for category, count in sorted(
                    summary["tool_error_categories"].items(), key=lambda x: x[1], reverse=True
                ):
                    analysis_lines.append(f"- **{category}**: {count} errors")
                analysis_lines.append("")

            # Unknown tools attempted
            if summary["unknown_tools_attempted"]:
                analysis_lines.extend(
                    [
                        "**Unknown Tools Attempted:**",
                    ]
                )
                # Show up to 10 unique unknown tools
                unique_tools = list(set(summary["unknown_tools_attempted"]))[:10]
                for tool in unique_tools:
                    analysis_lines.append(f"- `{tool}`")
                if len(summary["unknown_tools_attempted"]) > 10:
                    analysis_lines.append(
                        f"- ... and {len(summary['unknown_tools_attempted']) - 10} more"
                    )
                analysis_lines.append("")

            # Recovery failure rate
            if summary["recovery_failure_rate"] > 0:
                analysis_lines.extend(
                    [f"**Recovery Failure Rate:** {summary['recovery_failure_rate']:.1f}%", ""]
                )

            # Get sample errors for detailed analysis
            errors = error_logger.get_errors_for_analysis(limit=20)
            if errors:
                # Find most common error patterns
                error_patterns = {}
                for error in errors:
                    if error.get("type") == "tool_error":
                        key = f"{error.get('tool_name', 'unknown')}_{error.get('category', 'unknown')}"
                        if key not in error_patterns:
                            error_patterns[key] = {
                                "count": 0,
                                "tool": error.get("tool_name", "unknown"),
                                "category": error.get("category", "unknown"),
                                "sample_message": error.get("error_message", "No message"),
                                "suggestions": error.get("suggestions", []),
                            }
                        error_patterns[key]["count"] += 1

                if error_patterns:
                    analysis_lines.extend(
                        [
                            "**Common Error Patterns:**",
                        ]
                    )
                    # Show top 5 patterns
                    top_patterns = sorted(
                        error_patterns.items(), key=lambda x: x[1]["count"], reverse=True
                    )[:5]
                    for pattern_key, pattern_data in top_patterns:
                        analysis_lines.append(
                            f"- **{pattern_data['tool']}** ({pattern_data['category']}): "
                            f"{pattern_data['count']} occurrences"
                        )
                        if pattern_data["sample_message"]:
                            msg = pattern_data["sample_message"][:100]
                            if len(pattern_data["sample_message"]) > 100:
                                msg += "..."
                            analysis_lines.append(f"  - Sample: {msg}")
                        if pattern_data["suggestions"]:
                            analysis_lines.append(
                                f"  - Suggestion: {pattern_data['suggestions'][0]}"
                            )
                    analysis_lines.append("")

            return analysis_lines

        except Exception as e:
            logger.warning(f"Failed to get centralized error analysis: {e}")
            return []

    def _generate_error_reporting_section(self, actual_accomplishments: dict = None) -> list:
        """Render the centralized error-log analysis section, when errors exist."""
        # Get centralized error log analysis
        error_analysis_lines = self._get_centralized_error_analysis()
        if not error_analysis_lines:
            return []

        section_lines = [
            "## ⚠️ Error Analysis",
            "",
        ]
        section_lines.extend(error_analysis_lines)
        section_lines.extend(["", ""])
        return section_lines

    def _generate_next_steps_section(
        self, status: str, actual_accomplishments: dict = None
    ) -> list:
        """Generate next steps section based on actual status and context."""
        section_lines = []

        if status == "success":
            section_lines.extend(
                [
                    "## 🚀 Project Ready",
                    "",
                    "- ✅ Project has been successfully set up and tested",
                    "- ✅ All dependencies are installed and configured",
                    "- ✅ Development environment is ready for use",
                    "- 🎯 **Next Steps:** You can now start development or deployment",
                    "",
                ]
            )
        else:
            section_lines.extend(
                [
                    "## ❌ Setup Issues",
                    "",
                    "- ❌ Project setup encountered significant problems",
                    "- 📋 Check error logs and dependency requirements",
                    "- 🔧 Manual troubleshooting may be required",
                ]
            )

            # Add specific recommendations based on what failed
            if actual_accomplishments:
                if not actual_accomplishments.get("repository_cloned"):
                    section_lines.append(
                        "- 📥 **Critical:** Repository clone failed - check URL and access"
                    )
                elif not actual_accomplishments.get("build_success"):
                    section_lines.append(
                        "- 🔨 **Critical:** Build compilation failed - check dependencies"
                    )

            section_lines.append("")

        return section_lines

    def _persist_report_metrics(self, metrics: dict) -> None:
        """Write the structured metrics artifact for the web read model.
        Best-effort: never fail report generation on a metrics write error."""
        if not self.docker_orchestrator:
            return
        try:
            import json as _json
            import os as _os

            from sag.tools.report_metrics import METRICS_PATH

            parent = _os.path.dirname(METRICS_PATH)
            if parent:
                self.docker_orchestrator.execute_command(f"mkdir -p {parent}")

            body = _json.dumps(metrics, indent=2)
            delimiter = f"EOF_METRICS_{abs(hash(body)) % 10000}"
            command = f"cat > {METRICS_PATH} << '{delimiter}'\n{body}\n{delimiter}"
            self.docker_orchestrator.execute_command(command)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Failed to persist report metrics: {exc}")

    def _reactor_status_from_history(self, test_history: dict) -> dict:
        """Flatten reactor_summary records from test history into {label: status}.

        Labels are the raw Maven reactor display names (the module <name>, e.g.
        "Apache Kafka :: Connect :: API"); the metrics assembler normalizes both
        sides so these reconcile with the path-derived scan keys.
        """
        status: dict = {}
        records = (test_history or {}).get("reactor_records") or []
        for rec in records:
            label = str(rec.get("module") or "").strip()
            state = str(rec.get("status") or "").strip().lower()
            if label and state:
                status[label] = state
        return status

    def _build_module_metrics(self, test_history: dict, *, generated_at: str):
        """Assemble the per-module metrics dict, or None when unavailable.

        Memoized per report run: it is called once for persistence and again for
        the markdown breakdown; each call would otherwise re-scan every module in
        the container. ReportTool is created per report, so caching on self is
        safe (the result is identical within one generation)."""
        cached = getattr(self, "_module_metrics_cache", _MODULE_METRICS_UNSET)
        if cached is not _MODULE_METRICS_UNSET:
            return cached
        result = self._compute_module_metrics(test_history, generated_at=generated_at)
        self._module_metrics_cache = result
        return result

    def _is_python_project(self, project_info: Optional[dict] = None) -> bool:
        """True when the project under report is python (pytest-based).

        Physical detection first (same rationale as _compute_module_metrics:
        project_info.build_system is often "Unknown" at report time), falling
        back to the reported build system when no validator is wired.
        """
        detect = getattr(self.physical_validator, "_detect_build_system", None)
        if callable(detect):
            try:
                project_dir = (project_info or {}).get("directory") or "/workspace"
                detected = str(detect(project_dir) or "").strip().lower()
                if detected and detected != "unknown":
                    return detected == "python"
            except Exception as exc:
                logger.debug(f"_detect_build_system failed: {exc}")
        reported = str((project_info or {}).get("build_system") or "").strip().lower()
        return reported in ("python", "pip/poetry")

    def _compute_module_metrics(self, test_history: dict, *, generated_at: str):
        validator = getattr(self, "physical_validator", None)
        if validator is None:
            return None
        project_info = self._get_project_info() or {}
        project_dir = project_info.get("directory") or "/workspace"
        # Detect the build system PHYSICALLY (presence of pom.xml vs
        # build.gradle[.kts]). project_info.build_system is often "Unknown" at
        # report time; trusting it defaulted Gradle projects to maven, so
        # scan_modules looked for pom.xml/target and found nothing (live caffeine
        # run: a Gradle multi-project collapsed to 1 maven module, 0 classes).
        build_system = ""
        detect = getattr(validator, "_detect_build_system", None)
        if callable(detect):
            try:
                build_system = str(detect(project_dir) or "").strip().lower()
            except Exception as exc:
                logger.debug(f"_detect_build_system failed: {exc}")
        if build_system == "python":
            # Python project (pyproject.toml/setup.py/requirements.txt markers):
            # the module scan speaks Java (.class/jar globs, surefire/gradle
            # report dirs), so its counts are meaningless here. Live pyyaml run:
            # python fell through the maven fallback below and the scan produced
            # "🧩 Modules: 0 built / 1 detected" plus a bogus module-derived
            # build_modules_incomplete while 1287/1287 pytest tests had run.
            # None suppresses the modules line, the breakdown section, and the
            # module-derived conflicts — v1 python scope is single-package
            # (packages-as-modules is future work).
            return None
        if build_system not in ("maven", "gradle"):
            # Fall back to the reported build system, then maven as last resort.
            reported = str(project_info.get("build_system") or "").strip().lower()
            if reported in ("python", "pip/poetry"):
                # Reported-python with no physical maven/gradle markers: same
                # suppression as detected-python (the 'else maven' fallback was
                # the live-run bug).
                return None
            build_system = reported if reported in ("maven", "gradle") else "maven"
        try:
            modules = validator.scan_modules(project_dir, build_system)
        except Exception as exc:
            logger.debug(f"scan_modules failed: {exc}")
            return None
        if not modules:
            return None

        tests: dict = {}
        for m in modules:
            try:
                parsed = validator.parse_module_test_reports(
                    f"{project_dir}/{m['path']}" if m["path"] != "." else project_dir,
                    m.get("report_dirs") or [],
                )
            except Exception as exc:
                logger.debug(f"parse_module_test_reports failed for {m.get('path')}: {exc}")
                parsed = {}
            if parsed:
                tests[m["path"]] = parsed

        reactor_status = self._reactor_status_from_history(test_history)

        # No live reactor summary: narrow the detected set to the ACTIVE
        # reactor-declared modules (root + root-pom active, profile-stripped
        # <modules>) so standalone non-reactor poms (e.g. commons-chain's apps/*)
        # and pom-disabled modules are not counted as detected. The reactor-summary
        # path is already authoritative inside assemble_module_metrics.
        if not reactor_status and build_system == "maven":
            active_dirs = None
            try:
                active_dirs = validator._active_maven_module_dirs(project_dir)
            except Exception as exc:
                logger.debug(f"_active_maven_module_dirs failed: {exc}")
            if active_dirs:
                root = project_dir.rstrip("/")
                active_rel = {
                    (d.rstrip("/")[len(root):].strip("/") or ".") for d in active_dirs
                }
                # Keep the active-declared modules AND any scanned module that
                # actually produced compiled artifacts. The latter matters when the
                # build happened in submodules with no captured reactor summary
                # (e.g. carbondata's profile-gated modules): those built modules
                # must still be counted, not collapsed to "0/1". A module that is
                # neither declared nor produced artifacts (e.g. commons-chain's
                # standalone apps/*) stays excluded.
                modules = [
                    m
                    for m in modules
                    if str(m.get("path") or ".") in active_rel
                    or (m.get("class_count") or 0) > 0
                    or (m.get("jar_count") or 0) > 0
                ] or modules

        build_error_samples: dict = {}  # populated by Maven error parsing when available
        return assemble_module_metrics(
            modules=modules,
            reactor_status=reactor_status,
            tests=tests,
            build_systems=[build_system],
            build_error_samples=build_error_samples,
            generated_at=generated_at,
        )

    def _persist_module_metrics(self, metrics: dict) -> None:
        """Best-effort write of module_metrics.json (never blocks report gen)."""
        if not metrics or not self.docker_orchestrator:
            return
        try:
            import json as _json

            payload = _json.dumps(metrics, indent=2)
            delimiter = "SAG_MODULE_METRICS_EOF"
            cmd = (
                f"mkdir -p /workspace/.setup_agent && "
                f"cat > {MODULE_METRICS_PATH} <<'{delimiter}'\n{payload}\n{delimiter}"
            )
            self.docker_orchestrator.execute_command(cmd)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"Failed to persist module metrics: {exc}")

    def _render_submodule_breakdown(self, module_metrics: dict) -> List[str]:
        """Markdown 'Submodule Breakdown' section; [] for single-module projects."""
        if not module_metrics:
            return []
        summary = module_metrics.get("module_summary") or {}
        modules = module_metrics.get("modules") or []
        if summary.get("single_module") or len(modules) <= 1:
            return []

        def rank(m):
            if m.get("build_status") == "failure":
                return 0
            if (m.get("failing_count") or 0) > 0:
                return 1
            if m.get("build_status") == "skipped":
                return 2
            return 3

        lines = ["", "## 🧩 Submodule Breakdown", ""]
        lines.append(
            f"{summary.get('modules_built', 0)} built / "
            f"{summary.get('modules_total', 0)} detected · "
            f"{summary.get('modules_tested', 0)} tested · "
            f"{summary.get('modules_not_tested', 0)} not tested · "
            f"{summary.get('modules_failed', 0)} failed · "
            f"{summary.get('modules_skipped', 0)} skipped · "
            f"test failures in {summary.get('modules_with_test_failures', 0)}"
        )
        lines.append("")
        lines.append("| Module | Build | Tests (pass/fail/skip) |")
        lines.append("|---|---|---|")
        ordered = sorted(modules, key=rank)
        shown = ordered[:20]
        for m in shown:
            tp = m.get("tests_passed")
            tf = m.get("tests_failed")
            ts = m.get("tests_skipped")
            tests = (
                f"{tp if tp is not None else '—'}/"
                f"{tf if tf is not None else '—'}/"
                f"{ts if ts is not None else '—'}"
            )
            lines.append(
                f"| `{m.get('name')}` | {str(m.get('build_status', 'unknown')).upper()} | {tests} |"
            )
        if len(ordered) > len(shown):
            lines.append("")
            lines.append(
                f"_+{len(ordered) - len(shown)} more modules — full per-module data in "
                f"`/workspace/.setup_agent/module_metrics.json`_"
            )
        return lines

    def _save_markdown_report(self, markdown_content: str, timestamp: str, report_filename: str):
        """Save markdown report to workspace using here-doc for safe handling."""

        try:
            if self.docker_orchestrator:
                # Clean up any old report files first to prevent accumulation
                try:
                    cleanup_cmd = "rm -f /workspace/setup_report.md /workspace/final_report.md 2>/dev/null || true"
                    self.docker_orchestrator.execute_command(cleanup_cmd)
                    logger.debug("Cleaned up old report files")
                except:
                    pass  # Non-critical if cleanup fails

                # Use provided consistent filename
                filepath = f"/workspace/{report_filename}"

                # Use here-doc for safe content writing (no escaping needed)
                # Generate a unique delimiter to avoid conflicts with content
                delimiter = f"EOF_{hash(markdown_content) % 10000}"

                # Create here-doc command
                command = f"cat > {filepath} << '{delimiter}'\n{markdown_content}\n{delimiter}"

                result = self.docker_orchestrator.execute_command(command)

                # Check result using exit_code as primary indicator
                if result.get("exit_code") == 0:
                    logger.info(f"✅ Markdown report saved to: {filepath}")
                else:
                    # Fallback to old method if here-doc fails
                    logger.warning(f"⚠️ Here-doc failed, trying fallback method")
                    self._save_markdown_report_fallback(markdown_content, filepath)
            else:
                logger.warning(
                    "⚠️ Docker orchestrator not available, skipping markdown file creation"
                )

        except Exception as e:
            logger.error(f"❌ Error saving markdown report: {e}")
            # Try fallback method on any exception
            if self.docker_orchestrator:
                try:
                    self._save_markdown_report_fallback(
                        markdown_content, f"/workspace/{report_filename}"
                    )
                except Exception as fallback_error:
                    logger.error(f"❌ Fallback method also failed: {fallback_error}")

    def _save_markdown_report_fallback(self, markdown_content: str, filepath: str):
        """
        Fallback method for saving markdown report using base64 encoding.
        This method is more reliable for content with special characters.
        """
        try:
            import base64

            # Encode content to base64 to avoid shell escaping issues
            encoded_content = base64.b64encode(markdown_content.encode("utf-8")).decode("ascii")

            # Write using base64 decode
            command = f"echo '{encoded_content}' | base64 -d > {filepath}"
            result = self.docker_orchestrator.execute_command(command)

            if result.get("exit_code") == 0:
                logger.info(f"✅ Markdown report saved via fallback to: {filepath}")
            else:
                logger.error(f"❌ Fallback method failed: {result.get('output', 'Unknown error')}")

        except Exception as e:
            logger.error(f"❌ Base64 fallback failed: {e}")

    # ==================== NEW IMPROVED REPORT RENDERING METHODS ====================

    def _get_setup_agent_version(self) -> str:
        """Get the Setup-Agent version used in generated reports."""
        return __version__

    def _render_enhanced_header(
        self, timestamp: str, status: str, project_info: dict, snapshot: dict = None
    ) -> List[str]:
        """Render enhanced header with inline project information."""
        # Get Setup-Agent version
        version = self._get_setup_agent_version()
        version_text = f" v{version}" if version != "unknown" else ""

        lines = [f"# 🎯 Project Setup Report{version_text}", ""]

        # Extract project name from directory
        project_name = "Project"
        if project_info and project_info.get("directory"):
            project_dir = project_info["directory"]
            if "/" in project_dir:
                project_name = project_dir.split("/")[-1].title()

        # Create inline project info
        project_type = project_info.get("type", "Unknown") if project_info else "Unknown"
        build_system = project_info.get("build_system", "Unknown") if project_info else "Unknown"

        # Extract Java version if available
        java_version = ""
        if snapshot:
            for task in (
                self.context_manager.load_trunk_context().todo_list if self.context_manager else []
            ):
                if "java" in task.description.lower() and task.key_results:
                    if "java_version" in task.key_results:
                        import re

                        match = re.search(
                            r'java_version[\'"]?:\s*[\'"]?(\d+(?:\.\d+)?)', task.key_results
                        )
                        if match:
                            java_version = f" | Java {match.group(1)}"
                        break

        lines.append(f"**{project_name}** | {project_type} | {build_system}{java_version}")
        lines.append(f"**Generated:** {timestamp}")

        evidence_result = (snapshot or {}).get("evidence_result") or {}
        if self._should_render_report_evidence_result(evidence_result):
            # The Result line consumes the verdict kernel (spec §6) with the
            # same inputs as the agent's final status — never the raw
            # evidence status alone (round-5 iceberg: report PARTIAL while
            # the CLI announced success).
            verdict = self._snapshot_kernel_verdict(snapshot).upper()
            icon = {
                "SUCCESS": "✅",
                "PARTIAL": "⚠️",
                "FAILED": "❌",
            }.get(verdict, "❔")
            result_msg = f"**Result:** {icon} {verdict}"
            evidence_details = self._render_markdown_evidence_details(
                evidence_result, snapshot=snapshot
            )
            lines.append(result_msg)
            lines.extend(evidence_details)
            lines.append("")
            return lines

        # Determine result message based on status and test metrics
        result_msg = f"**Result:** "
        if status.upper() == "SUCCESS":
            if snapshot:
                pass_rate = snapshot.get("status", {}).get("pass_pct", 0)
                result_msg += (
                    f"✅ SUCCESS (Build Passed, {format_percentage(pass_rate)} Tests Pass)"
                )
            else:
                result_msg += "✅ SUCCESS"
        else:
            result_msg += "❌ FAILED"
            if snapshot:
                phases = snapshot.get("phases", {})
                if not phases.get("build"):
                    result_msg += " (Build Failed)"
                elif not phases.get("test"):
                    result_msg += " (Tests Failed)"

        lines.extend([result_msg, ""])
        return lines

    def _render_summary_dashboard(self, snapshot: Dict[str, Any]) -> List[str]:
        """Render summary dashboard with key metrics in a visual box."""
        lines = ["## 📊 Summary Dashboard", ""]

        status = snapshot.get("status", {})
        phases = snapshot.get("phases", {})
        evidence = snapshot.get("physical_evidence", {})

        # Prepare values
        clone_status = "✅ Cloned successfully" if phases.get("clone") else "❌ Clone failed"

        build_status = "✅" if phases.get("build") else "❌"
        class_files = evidence.get("class_files", 0)
        jar_files = evidence.get("jar_files", 0)
        if class_files or jar_files:
            build_msg = f"{build_status} {class_files:,} classes, {jar_files} JARs"
        else:
            build_msg = f"{build_status} No artifacts"

        # Test metrics - now with accurate counting
        static_count = status.get("static_test_count", 0)
        method_count = status.get("method_count", 0)
        executed = status.get("tests_total", 0)
        raw_executed = status.get("tests_total_raw", 0)
        passed = status.get("tests_passed", 0)
        exec_rate = status.get("execution_rate")
        expansion_factor = status.get("expansion_factor")
        pass_rate = status.get("pass_pct")

        # Module build completeness (active modules built vs detected)
        modules_detected = status.get("modules_detected")
        modules_built = status.get("modules_built") or 0

        lines.append("### Build & Test Overview")
        lines.append("```")
        lines.append("┌─────────────────┬──────────────────────────────────┐")
        lines.append(f"│ Repository      │ {clone_status:<32} │")
        lines.append(f"│ Build           │ {build_msg:<32} │")

        if static_count:
            test_count_msg = f"📊 {static_count} test methods"
            lines.append(f"│ Total Tests     │ {test_count_msg:<32} │")

        if raw_executed and raw_executed > executed and expansion_factor:
            raw_msg = f"🔄 {raw_executed} raw runs (~{expansion_factor:.1f}x)"
            lines.append(f"│ Raw Executions  │ {raw_msg:<32} │")

        if exec_rate is not None and static_count:
            # Execution rate now reflects coverage of static test methods
            if executed > static_count:
                # Handle case where runtime discovered more tests than static analysis
                exec_icon = "✅"  # More tests run than expected is generally good
                exec_msg = f"{exec_icon} {format_percentage(exec_rate)} ({executed} run, {static_count} expected)"
            else:
                exec_icon = "✅" if exec_rate >= 95 else "⚠️" if exec_rate >= 80 else "❌"
                exec_msg = f"{exec_icon} {format_percentage(exec_rate)} ({executed}/{static_count})"
            lines.append(f"│ Execution Rate  │ {exec_msg:<32} │")

        if pass_rate is not None:
            pass_icon = "✅" if pass_rate >= 95 else "⚠️" if pass_rate >= 80 else "❌"
            pass_msg = f"{pass_icon} {format_percentage(pass_rate)} ({passed}/{executed} passed)"
            lines.append(f"│ Pass Rate       │ {pass_msg:<32} │")

        if modules_detected:
            mod_icon = (
                "✅" if modules_built >= modules_detected
                else "⚠️" if modules_built > 0 else "❌"
            )
            mod_msg = f"{mod_icon} {modules_built}/{modules_detected} built"
            lines.append(f"│ Module Coverage │ {mod_msg:<32} │")

            tested = status.get("modules_tested") or 0
            not_tested = status.get("modules_not_tested")
            if not_tested is None:
                not_tested = modules_detected - tested
            test_msg = f"🧪 {tested} tested / {not_tested} not tested"
            lines.append(f"│ Module Tests    │ {test_msg:<32} │")

        lines.append("└─────────────────┴──────────────────────────────────┘")
        lines.append("```")
        lines.append("")

        return lines

    def _render_detailed_test_analysis(self, snapshot: Dict[str, Any]) -> List[str]:
        """Render detailed test analysis with all metrics clearly displayed."""
        status = snapshot.get("status", {})

        # Skip if no tests were run
        if not status.get("tests_total"):
            return []

        lines = ["## 🧪 Detailed Test Analysis", ""]

        # Test Metrics Summary
        lines.extend(
            [
                "### Test Metrics Summary",
                "",
                "| Metric | Value | Calculation | Status |",
                "|--------|-------|-------------|--------|",
            ]
        )

        static_count = status.get("static_test_count", 0)
        method_count = status.get("method_count", 0)
        executed = status.get("tests_total", 0)
        raw_executed = status.get("tests_total_raw", 0)
        unique_executed = status.get("tests_unique", 0)
        passed = status.get("tests_passed", 0)
        exec_rate = status.get("execution_rate")
        pass_rate = status.get("pass_pct")
        expansion_factor = status.get("expansion_factor")

        # Static test methods discovered during analysis
        if static_count:
            lines.append(
                f"| **Total Test Methods** | {static_count} | @Test-style annotations discovered | 📊 |"
            )

        lines.append(
            f"| **Tests Executed** | {executed} | Runner XML count | {'✅' if pass_rate and pass_rate >= 95 else '⚠️' if pass_rate and pass_rate >= 80 else '❌' if pass_rate is not None else '📊'} |"
        )

        # Highlight method-level deduplication when parameterized/dynamic tests expand at runtime.
        if unique_executed and unique_executed != executed:
            lines.append(
                f"| **Unique Test Methods** | {unique_executed} | Normalized runtime method count | 📊 |"
            )

        if (
            raw_executed
            and unique_executed
            and raw_executed > unique_executed
            and expansion_factor
            and expansion_factor > 1
        ):
            lines.append(
                f"| **Parameterized Expansion** | ~{expansion_factor:.1f}x | {raw_executed} runner executions / {unique_executed} methods | 🔄 |"
            )

        lines.append(f"| **Tests Passed** | {passed} | Successful runner count | ✅ |")

        # Execution rate now measures coverage of declared test methods
        if exec_rate is not None and static_count:
            # Handle case where executed tests exceed static count (e.g., dynamically generated tests)
            coverage_count = unique_executed or executed
            if coverage_count > static_count:
                exec_icon = "✅"  # More tests run than expected is generally good
                lines.append(
                    f"| **Execution Rate** | {format_percentage(exec_rate)} | {coverage_count} tests run (exceeded {static_count} expected) | {exec_icon} |"
                )
                lines.append(
                    f"| | *Note:* | *Runtime discovered more tests than static analysis* | 📊 |"
                )
            else:
                exec_icon = "✅" if exec_rate >= 95 else "⚠️" if exec_rate >= 80 else "❌"
                actual_tests_run = int(exec_rate * static_count / 100)
                lines.append(
                    f"| **Execution Rate** | {format_percentage(exec_rate)} | {coverage_count} of {static_count} tests run | {exec_icon} |"
                )
                if exec_rate < 100:
                    skipped_est = static_count - actual_tests_run
                    if skipped_est > 0:
                        lines.append(f"| | *~{skipped_est} tests* | *possibly not executed* | ⚠️ |")

        if pass_rate is not None:
            pass_icon = "✅" if pass_rate >= 95 else "⚠️" if pass_rate >= 80 else "❌"
            lines.append(
                f"| **Pass Rate** | {format_percentage(pass_rate)} | {passed} ÷ {executed} | {pass_icon} |"
            )

        lines.append("")

        # Test Execution Breakdown
        lines.extend(
            [
                "### Test Execution Breakdown",
                "",
                "| Total Available | Executed | Passed | Failed | Errors | Skipped |",
                "|-----------------|----------|--------|--------|---------|---------|",
            ]
        )

        failed = status.get("tests_failed", 0)
        errors = status.get("tests_errors", 0)
        skipped = status.get("tests_skipped", 0)

        lines.append(
            f"| {static_count if static_count else 'N/A'} | {executed} | {passed} | {failed} | {errors} | {skipped} |"
        )
        lines.append("")

        # Module Coverage Analysis
        modules_expected = status.get("modules_expected")
        modules_seen = status.get("modules_seen", 0)
        skipped_modules = status.get("skipped_modules", [])

        if modules_expected or skipped_modules:
            lines.extend(["### Module Coverage Analysis", ""])

            if modules_expected:
                lines.append(f"- **Total Modules:** {modules_expected}")
                mod_pct = (modules_seen / modules_expected * 100) if modules_expected else 0
                lines.append(f"- **Executed Modules:** {modules_seen} ({mod_pct:.1f}%)")

            if skipped_modules:
                unexecuted_tests = 0
                if static_count and executed:
                    unexecuted_tests = static_count - executed

                lines.append(f"- **Skipped Modules:** {len(skipped_modules)}")
                if unexecuted_tests:
                    lines.append(f"  (containing ~{unexecuted_tests} unexecuted tests)")

                # List first few skipped modules
                for module in skipped_modules[:3]:
                    lines.append(f"  - {module}")
                if len(skipped_modules) > 3:
                    lines.append(f"  - [+{len(skipped_modules) - 3} more...]")

            lines.append("")

        return lines

    def _render_issues_recommendations(self, snapshot: Dict[str, Any]) -> List[str]:
        """Render issues and recommendations section."""
        lines = ["## 🚨 Issues & Recommendations", ""]

        status = snapshot.get("status", {})
        attention_raw = snapshot.get("attention", {}).get("raw", [])

        # Group by severity
        blockers = [item for item in attention_raw if item["severity"] == "BLOCKER"]
        warnings = [item for item in attention_raw if item["severity"] == "WARNING"]
        info_items = [item for item in attention_raw if item["severity"] == "INFO"]

        # Key Observations
        lines.extend(["### Key Observations"])

        pass_rate = status.get("pass_pct")
        exec_rate = status.get("execution_rate")
        expansion_factor = status.get("expansion_factor")

        if pass_rate and pass_rate >= 95:
            lines.append(
                f"- ✅ **High Pass Rate:** {format_percentage(pass_rate)} of executed tests passed"
            )
        elif pass_rate:
            lines.append(
                f"- ⚠️ **Pass Rate:** {format_percentage(pass_rate)} of executed tests passed"
            )

        if exec_rate:
            if exec_rate < 90:
                lines.append(
                    f"- ⚠️ **Low Execution Rate:** Only {format_percentage(exec_rate)} of available tests were run"
                )

        if expansion_factor and expansion_factor > 1:
            lines.append(
                f"- ℹ️ **Parameterized Tests Detected:** Runtime produced ~{expansion_factor:.1f}× more executions than unique tests"
            )

        modules_expected = status.get("modules_expected")
        modules_seen = status.get("modules_seen", 0)
        if modules_expected:
            coverage = modules_seen / modules_expected * 100
            if coverage < 80:
                lines.append(
                    f"- ⚠️ **Incomplete Coverage:** {coverage:.0f}% of modules were tested"
                )

        lines.append("")

        # Issues by severity
        if blockers:
            lines.extend([f"### Blockers ({len(blockers)})", ""])
            for item in blockers:
                lines.append(f"- {item['icon']} {item['message']}")
            lines.append("")
        else:
            lines.extend(["### Blockers (0)", "✅ No blocking issues", ""])

        if warnings:
            lines.extend([f"### Warnings ({len(warnings)})", ""])
            for item in warnings[:5]:
                lines.append(f"- {item['icon']} {item['message']}")
            if len(warnings) > 5:
                lines.append(f"- ... (+{len(warnings) - 5} more)")
            lines.append("")

        # Actionable Recommendations
        lines.extend(["### Actionable Recommendations"])

        if exec_rate and exec_rate < 90:
            skipped_modules = status.get("skipped_modules", [])[:3]
            if skipped_modules:
                modules_str = ",".join(skipped_modules)
                lines.append(f"1. **Increase Test Execution Rate**:")
                lines.append(f"   ```bash")
                lines.append(f"   mvn test -pl {modules_str}")
                lines.append(f"   ```")

        lines.append("2. **Run All Tests**:")
        lines.append("   ```bash")
        lines.append("   mvn clean test -DskipTests=false")
        lines.append("   ```")

        if warnings or blockers:
            lines.append("3. **Check Skipped Reasons**:")
            lines.append("   ```bash")
            lines.append('   mvn test -X | grep -i "skip\\|exclude"')
            lines.append("   ```")

        lines.append("")

        return lines

    def _render_task_progress(self, actual_accomplishments: dict = None) -> List[str]:
        """Render task progress in improved table format."""
        lines = ["## 📋 Task Progress", ""]

        if not self.context_manager:
            return lines

        try:
            trunk_context = self.context_manager.load_trunk_context()
            if not trunk_context or not trunk_context.todo_list:
                return lines

            lines.extend(
                ["| # | Task | Status | Key Result |", "|---|------|--------|------------|"]
            )

            for i, task in enumerate(trunk_context.todo_list, 1):
                status_icon = (
                    "✅"
                    if task.status.value == "completed"
                    else "🔄" if task.status.value == "in_progress" else "⏳"
                )

                # Extract key result
                key_result = ""
                if task.key_results:
                    # Highlight test metrics
                    if "static_test_count" in task.key_results:
                        import re

                        match = re.search(r"static_test_count=(\d+)", task.key_results)
                        if match:
                            key_result = f"**{match.group(1)} total @Test** found"
                    elif (
                        "test" in task.description.lower()
                        and "executed" in task.key_results.lower()
                    ):
                        # Extract execution metrics
                        import re

                        total_match = re.search(r'total_tests[\'"]?:\s*(\d+)', task.key_results)
                        if total_match:
                            total = total_match.group(1)
                            key_result = f"**{total} executed**"
                            # Try to add rate if static count available
                            if trunk_context.environment_summary.get("static_test_count"):
                                static = trunk_context.environment_summary["static_test_count"]
                                rate = int(total) / static * 100
                                key_result = f"**{total}/{static} executed ({rate:.1f}%)**"
                    elif task.key_results:
                        # Truncate long results
                        key_result = (
                            task.key_results[:50] + "..."
                            if len(task.key_results) > 50
                            else task.key_results
                        )

                # Use full task description or reasonable truncation
                task_desc = (
                    task.description[:100] + "..."
                    if len(task.description) > 100
                    else task.description
                )

                lines.append(f"| {i} | {task_desc} | {status_icon} | {key_result} |")

            lines.append("")

        except Exception as e:
            logger.warning(f"Could not generate task progress: {e}")

        return lines

    def _render_execution_details_simplified(
        self, snapshot: dict = None, execution_metrics: dict = None
    ) -> List[str]:
        """Render simplified execution details."""
        lines = ["## 🛠 Execution Details", ""]

        # Get command info from snapshot
        if snapshot:
            last_cmd = snapshot.get("last_command", {})
            if last_cmd:
                tool = last_cmd.get("tool", "maven")
                command = last_cmd.get("command", "N/A")
                workdir = last_cmd.get("workdir", "/workspace")

                lines.append(
                    f"**Build Command:** `{command if 'install' in command or 'compile' in command else 'mvn clean install -DskipTests'}`"
                )
                lines.append(f"**Test Command:** `{command if 'test' in command else 'mvn test'}`")

        # Add runtime metrics
        if execution_metrics:
            runtime = execution_metrics.get("total_runtime", 0)
            iterations = execution_metrics.get("total_iterations", 0)
            total_thoughts = execution_metrics.get("total_thoughts", 0)
            total_actions = execution_metrics.get("total_actions", 0)
            successful_actions = execution_metrics.get("successful_actions", 0)
            success_rate = execution_metrics.get("success_rate", 0)

            # Main metrics line with thoughts and actions
            if successful_actions is not None and total_actions:
                lines.append(
                    f"**Runtime:** {runtime:.1f} minutes | {iterations} iterations | "
                    f"{total_thoughts} thoughts | {total_actions} actions | "
                    f"success rate {successful_actions}/{total_actions} ({success_rate:.0f}%)"
                )
            else:
                lines.append(
                    f"**Runtime:** {runtime:.1f} minutes | {iterations} iterations | "
                    f"{total_thoughts} thoughts | {total_actions} actions | "
                    f"{success_rate:.0f}% success rate"
                )
            lines.append("")

            # Add test metrics if available from snapshot
            if snapshot:
                status = snapshot.get("status", {})
                static_test_count = status.get("static_test_count")
                tests_total = status.get("tests_total")
                if static_test_count:
                    test_coverage_line = (
                        f"**Test Coverage:** {tests_total or 0}/{static_test_count} tests executed"
                    )
                    if tests_total and static_test_count:
                        execution_rate = (tests_total / static_test_count) * 100
                        test_coverage_line += f" ({execution_rate:.1f}% execution rate)"
                    lines.append(test_coverage_line)
                    lines.append("")

            # Tool usage summary
            tools_used = execution_metrics.get("tools_used", {})
            if tools_used:
                tools_summary = []
                for tool, count in sorted(tools_used.items(), key=lambda x: x[1], reverse=True):
                    tool_name = tool.replace("_", " ").title()
                    tools_summary.append(f"{tool_name} ({count})")

                lines.append("### Tool Usage")
                lines.append(", ".join(tools_summary))
                lines.append("")

        return lines

    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool."""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["generate"],
                    "description": "Action to perform (always 'generate' for final report)",
                    "default": "generate",
                },
                "summary": {
                    "type": "string",
                    "description": "Brief summary of what was accomplished",
                    "default": None,
                },
                "status": {
                    "type": "string",
                    "enum": ["success", "partial", "failed"],
                    "description": "Overall status of the setup process",
                    "default": "success",
                },
                "details": {
                    "type": "string",
                    "description": "Additional details about the setup process",
                    "default": None,
                },
                "evidence_status": {
                    "type": "string",
                    "enum": ["success", "partial", "blocked", "conflict", "unknown"],
                    "description": "Evidence-backed result status for the report",
                    "default": None,
                },
                "test_stats": {
                    "type": "object",
                    "description": "Evidence-backed test counts for the report",
                    "default": None,
                    "properties": {
                        "discovered": {"type": "integer"},
                        "executed": {"type": "integer"},
                        "passed": {"type": "integer"},
                        "failed": {"type": "integer"},
                        "skipped": {"type": "integer"},
                    },
                },
                "conflicts": {
                    "type": "array",
                    "description": "Stable evidence conflict identifiers",
                    "default": None,
                    "items": {"type": "string"},
                },
                "evidence_refs": {
                    "type": "array",
                    "description": "Traceable evidence references such as report or artifact paths",
                    "default": None,
                    "items": {"type": "string"},
                },
            },
            "required": ["action"],
        }

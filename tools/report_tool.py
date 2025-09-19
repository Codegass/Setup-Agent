"""Report tool for generating task summaries and marking completion."""

import json
from typing import Dict, Any, Iterable, List, Optional, Tuple
from datetime import datetime

from loguru import logger

from .base import BaseTool, ToolResult
from reporting import render_condensed_summary, truncate_list, format_percentage


class ReportTool(BaseTool):
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

    def __init__(self, docker_orchestrator=None, execution_history_callback=None, context_manager=None, physical_validator=None):
        super().__init__(
            name="report",
            description="Generate comprehensive project setup report and mark task as complete. "
            "Creates both console output and a Markdown file in /workspace. "
            "Use this tool when all main tasks are finished to summarize the work done.",
        )
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
        **kwargs
    ) -> ToolResult:
        """
        Generate project setup report and mark completion.
        
        Args:
            action: Action to perform ('generate' for final report)
            summary: Brief summary of what was accomplished
            status: Overall status ('success' or 'fail') - REQUIRED
                   - 'success': Build validation passed AND test pass rate > 80%
                   - 'fail': Build failed OR test report not found OR test pass rate <= 80%
            details: Additional details about the setup process
        """
        
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
                    f"     • 'success': Build passed AND test pass rate > 80%\n"
                    f"     • 'fail': Build failed OR tests not found OR pass rate <= 80%\n"
                    f"  - details (optional): Additional details about the setup\n\n"
                    f"Example: report(action='generate')\n"
                    f"Example: report(action='generate', summary='Project built successfully', status='success')\n"
                    f"Example: report(action='generate', summary='Project built successfully', status='success', details='All build and test tasks completed successfully')"
                ),
                error=f"Invalid parameters: {invalid_params}"
            )

        if not status:
            return ToolResult(
                success=False,
                output="❌ Missing required parameter: 'status'. Must be either 'success' or 'fail'\n"
                      "• 'success': Build passed AND test pass rate > 80%\n"
                      "• 'fail': Build failed OR tests not found OR pass rate <= 80%",
                error="Missing required parameter: status"
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
                        error_code="PREREQUISITE_TASKS_INCOMPLETE"
                    )
                
                (
                    report,
                    verified_status,
                    report_filename,
                    actual_accomplishments,
                    report_snapshot,
                ) = self._generate_comprehensive_report(summary, status, details)
                
                # Mark this as a completion signal for the ReAct engine
                metadata = {
                    "task_completed": True,
                    "completion_signal": True,
                    "status": status,
                    "verified_status": verified_status,  # Include the verified status
                    "timestamp": datetime.now().isoformat(),
                    "report_snapshot": report_snapshot,
                }

                # ENHANCED: Provide condensed output for logs to reduce noise
                # Full report is saved to markdown file, logs get summary only
                condensed_output = self._generate_condensed_log_output(
                    verified_status,
                    report_filename,
                    actual_accomplishments,
                    report_snapshot,
                )

                return ToolResult(
                    success=True,
                    output=condensed_output,
                    metadata=metadata,
                    documentation_links=[],
                    raw_data={
                        "full_report": report,
                        "report_snapshot": report_snapshot,
                    }  # Store full report in metadata
                )
            else:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Invalid action '{action}'. Use 'generate' to create report.",
                    suggestions=["Use action='generate' to create the final report"]
                )
                
        except Exception as e:
            logger.error(f"Failed to generate report: {e}")
            return ToolResult(
                success=False,
                output="",
                error=f"Report generation failed: {str(e)}",
                suggestions=["Check if all required information is available"]
            )

    def _generate_comprehensive_report(self, summary: str, status: str, details: str) -> Tuple[str, str, str, dict, dict]:
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
        
        # CRITICAL: Update phase status based on physical validation results
        if actual_accomplishments:
            # Update phase status with detailed validation results
            clone_success = actual_accomplishments.get('repository_cloned', False)
            build_success = actual_accomplishments.get('build_success', False) 
            test_success = actual_accomplishments.get('test_success', False)
            
            execution_metrics['phases']['clone']['status'] = clone_success
            execution_metrics['phases']['analyze']['status'] = clone_success  # Can only analyze if cloned
            execution_metrics['phases']['build']['status'] = build_success
            execution_metrics['phases']['test']['status'] = test_success
            
            # Add validation evidence to phases
            if 'physical_validation' in actual_accomplishments:
                physical_data = actual_accomplishments['physical_validation']
                execution_metrics['phases']['build']['evidence'] = {
                    'class_files': physical_data.get('class_files', 0),
                    'jar_files': physical_data.get('jar_files', 0),
                    'recent_compilation': physical_data.get('recent_compilation', False),
                    'missing_classes': physical_data.get('missing_classes', 0)
                }
                
                if 'test_analysis' in physical_data:
                    test_data = physical_data['test_analysis']
                    execution_metrics['phases']['test']['evidence'] = {
                        'total_tests': test_data.get('total_tests', 0),
                        'passed_tests': test_data.get('passed_tests', 0),
                        'failed_tests': test_data.get('failed_tests', 0),
                        'error_tests': test_data.get('error_tests', 0),
                        'report_files_count': test_data.get('report_files_count', 0)
                    }
            
            # Estimate phase durations based on execution history if available
            self._estimate_phase_durations(execution_metrics, actual_accomplishments)
            
            logger.info(f"📊 Phase status updated from physical validation: Clone={clone_success}, "
                       f"Build={build_success}, Test={test_success}")
        
        report_snapshot = self._build_report_snapshot(
            verified_status,
            report_filename,
            project_info or {},
            actual_accomplishments,
            execution_metrics,
        )

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
        
        return console_report, verified_status, report_filename, actual_accomplishments, report_snapshot

    def _collect_simple_status_from_tasks(self) -> dict:
        """
        Collect simple three-phase status directly from trunk context tasks.
        This is the simplified version that avoids complex execution history parsing.
        
        Returns:
            dict: {
                'clone_success': bool,
                'build_success': bool, 
                'test_success': bool,
                'build_errors': [],
                'failing_tests': [],
                'project_info': dict
            }
        """
        simple_status = {
            'clone_success': False,
            'build_success': False,
            'test_success': False,
            'build_errors': [],
            'failing_tests': [],
            'project_info': {}
        }
        
        try:
            if not self.context_manager:
                return simple_status
                
            trunk_context = self.context_manager.load_trunk_context()
            if not trunk_context or not hasattr(trunk_context, 'todo_list'):
                return simple_status
            
            # Analyze completed tasks for three core phases
            for task in trunk_context.todo_list:
                if task.status.value == 'completed' and task.key_results:
                    task_desc = task.description.lower()
                    key_results = task.key_results.lower()
                    
                    # Phase 1: Repository Clone
                    if any(keyword in task_desc for keyword in ['clone', 'repository', 'setup']):
                        clone_indicators = ['project_type=', 'repository', 'cloned', 'path=/workspace/', 'repo_dir=']
                        if any(indicator in key_results for indicator in clone_indicators):
                            simple_status['clone_success'] = True
                    
                    # Phase 2: Build/Compilation
                    if any(keyword in task_desc for keyword in ['compile', 'build', 'maven', 'gradle']):
                        build_indicators = ['modules_compiled:', 'build_status=success', 'build_success=true', 'compiled']
                        if any(indicator in key_results for indicator in build_indicators):
                            simple_status['build_success'] = True
                        # TODO: Extract build errors if any
                    
                    # Phase 3: Testing
                    if any(keyword in task_desc for keyword in ['test', 'run test']):
                        test_indicators = ['test_status=success', 'tests_passed=true', 'all tests']
                        if any(indicator in key_results for indicator in test_indicators):
                            simple_status['test_success'] = True
                        # TODO: Extract failing tests if any
            
            # Get project info using existing method
            simple_status['project_info'] = self._get_project_info()
            
            logger.debug(f"🔍 Simple status: Clone={simple_status['clone_success']}, "
                       f"Build={simple_status['build_success']}, Test={simple_status['test_success']}")
                       
        except Exception as e:
            logger.warning(f"Failed to collect simple status from tasks: {e}")
            
        return simple_status

    def _render_simple_summary_top(self, simple_status: dict) -> str:
        """
        Render the simple three-phase summary at the top of the report.
        
        Args:
            simple_status: Status dict from _collect_simple_status_from_tasks
            
        Returns:
            str: Formatted simple summary for console output
        """
        lines = [
            "📋 SETUP RESULT SUMMARY",
            "=" * 50,
        ]
        
        # Phase 1: Repository Clone
        if simple_status['clone_success']:
            lines.append("✅ Repository Clone: SUCCESS")
        else:
            lines.append("❌ Repository Clone: FAILED")
        
        # Phase 2: Project Build
        if simple_status['build_success']:
            lines.append("✅ Project Build: SUCCESS")
        else:
            lines.append("❌ Project Build: FAILED")
            if simple_status['build_errors']:
                error_count = len(simple_status['build_errors'])
                lines.append(f"   └─ {error_count} build error(s) detected")
        
        # Phase 3: Test Suite
        if simple_status['test_success']:
            lines.append("✅ Test Suite: SUCCESS")
        else:
            lines.append("❌ Test Suite: FAILED")
            if simple_status['failing_tests']:
                test_count = len(simple_status['failing_tests'])
                lines.append(f"   └─ {test_count} test case(s) failed")
        
        # Project Information
        project_info = simple_status.get('project_info', {})
        if project_info.get('type'):
            lines.append(f"📂 Project Type: {project_info['type']}")
        
        # Next Steps Recommendations
        lines.append("")
        lines.append("💡 Next Steps:")
        
        if not simple_status['clone_success']:
            lines.append("   → Fix repository access and retry clone")
        elif not simple_status['build_success']:
            lines.append("   → Check build dependencies and fix compilation errors")
        elif not simple_status['test_success']:
            lines.append("   → Review test failures and fix issues")
            # TODO: Add specific Maven/Gradle recovery commands
            build_system = project_info.get('build_system', '').lower()
            if 'maven' in build_system:
                lines.append("   → Continue with remaining tests: mvn -fae test")
            elif 'gradle' in build_system:
                lines.append("   → Continue with remaining tests: ./gradlew test --continue")
        else:
            lines.append("   → Project is ready for development/deployment! 🎉")
        
        lines.extend(["", "=" * 50, ""])
        
        return "\n".join(lines)

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
            'ignored_lines': 0,
            'last_cmd': {},
            'aggregate': {},
            'per_module': {},
            'exclusions': {'tests': [], 'modules': []},
            'failed_tests': [],
            'flags': {},
        }

        modules_seen: Dict[str, Dict[str, Any]] = {}
        aggregate_entry: Dict[str, Any] = {}
        skipped_modules: set[str] = set()
        excluded_tests: set[str] = set()
        excluded_modules: set[str] = set()
        failed_tests: set[str] = set()
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

            total = pick(['total', 'tests_total', 'total_tests'])
            failed = pick(['failed', 'failures', 'tests_failed', 'tests_failures']) or 0
            errors = pick(['errors', 'error', 'tests_errors']) or 0
            skipped = pick(['skipped', 'tests_skipped']) or 0
            passed = pick(['passed', 'passes', 'tests_passed'])

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
                'total': cast(total),
                'passed': cast(passed),
                'failed': cast(failed),
                'error': cast(errors),
                'skipped': cast(skipped),
                'pass_pct': pass_pct,
            }

        for raw_line in raw_lines:
            line = raw_line.strip()
            if not line:
                continue
            if len(line) > max_bytes:
                history['ignored_lines'] += 1
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                history['ignored_lines'] += 1
                continue

            event_type = entry.get('event') or 'legacy_session'

            if event_type == 'test_module_summary':
                module_name = entry.get('module') or entry.get('module_name')
                if not module_name:
                    continue
                counts = normalize_tests(entry.get('tests', entry))
                modules_seen[module_name] = {
                    'total': counts['total'],
                    'passed': counts['passed'],
                    'failed': counts['failed'],
                    'error': counts['error'],
                    'skipped': counts['skipped'],
                    'pass_pct': counts['pass_pct'],
                }

                excluded = entry.get('exclusions', {})
                if isinstance(excluded, dict):
                    excluded_tests.update(excluded.get('tests', []) or [])
                    excluded_modules.update(excluded.get('modules', []) or [])
                skipped_modules.update(entry.get('skipped_modules', []) or [])
                failed_tests.update(entry.get('failed_tests', []) or [])
                continue

            if event_type in {'test_session_end', 'legacy_session'}:
                aggregate_entry = entry
                counts = normalize_tests(entry.get('tests', entry))
                aggregate_entry['_normalized_tests'] = counts

                modules_expected = entry.get('modules_expected', modules_expected)
                if entry.get('modules_seen'):
                    try:
                        seen = int(entry['modules_seen'])
                        aggregate_entry['_modules_seen'] = seen
                    except (TypeError, ValueError):
                        pass

                skipped_modules.update(entry.get('skipped_modules', []) or [])
                failed_tests.update(entry.get('failed_tests', []) or [])

                exclusions_field = entry.get('exclusions')
                if isinstance(exclusions_field, dict):
                    excluded_tests.update(exclusions_field.get('tests', []) or [])
                    excluded_modules.update(exclusions_field.get('modules', []) or [])
                else:
                    excluded_tests.update(entry.get('excluded_tests', []) or [])
                    excluded_modules.update(entry.get('excluded_modules', []) or [])

                history['last_cmd'] = {
                    'tool': entry.get('tool'),
                    'workdir': entry.get('working_directory'),
                    'exit_code': entry.get('exit_code'),
                    'command': entry.get('command'),
                    'fail_at_end': entry.get('fail_at_end')
                }

                # Infer fail_at_end from command if not explicit
                if history['last_cmd'].get('fail_at_end') is None:
                    command_str = entry.get('command') or ''
                    history['last_cmd']['fail_at_end'] = '--fail-at-end' in command_str or ' -fae' in command_str

        if not modules_seen and not aggregate_entry:
            return {}

        aggregate_counts = aggregate_entry.get('_normalized_tests', {}) if aggregate_entry else {}
        aggregate: Dict[str, Any] = {
            'modules_expected': modules_expected,
            'skipped_modules': sorted(skipped_modules) if skipped_modules else [],
            'tests': {
                'total': aggregate_counts.get('total'),
                'passed': aggregate_counts.get('passed'),
                'failed': aggregate_counts.get('failed'),
                'error': aggregate_counts.get('error'),
                'skipped': aggregate_counts.get('skipped'),
            },
            'pass_pct': aggregate_counts.get('pass_pct'),
        }

        modules_seen_count = aggregate_entry.get('_modules_seen') if aggregate_entry else None
        if modules_seen_count is None:
            modules_seen_count = len(modules_seen)
        aggregate['modules_seen'] = modules_seen_count

        # Detect inconsistencies between per-module totals and aggregate
        if aggregate_counts.get('total') is not None and modules_seen:
            module_total = 0.0
            for module_info in modules_seen.values():
                if module_info.get('total') is not None:
                    module_total += module_info['total']
            try:
                aggregate['inconsistent'] = abs(module_total - aggregate_counts['total']) > 0.5
            except TypeError:
                aggregate['inconsistent'] = True

        history['aggregate'] = aggregate
        history['per_module'] = modules_seen
        history['exclusions']['tests'] = sorted(excluded_tests)
        history['exclusions']['modules'] = sorted(excluded_modules)
        history['failed_tests'] = sorted(failed_tests)
        history['flags']['fail_at_end'] = history['last_cmd'].get('fail_at_end') if history['last_cmd'] else None

        return history

    def _build_report_snapshot(
        self,
        verified_status: str,
        report_filename: str,
        project_info: dict,
        actual_accomplishments: dict,
        execution_metrics: dict,
    ) -> Dict[str, Any]:
        """Create a normalized snapshot used for rendering condensed and markdown reports."""

        actual_accomplishments = actual_accomplishments or {}
        execution_metrics = execution_metrics or {}

        test_history = execution_metrics.get('test_history', {}) or {}
        aggregate = test_history.get('aggregate', {}) or {}
        per_module = test_history.get('per_module', {}) or {}

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

        tests_counts = aggregate.get('tests', {}) or {}
        tests_total = tests_counts.get('total')
        tests_failed = tests_counts.get('failed')
        tests_error = tests_counts.get('error')
        tests_skipped = tests_counts.get('skipped')
        tests_passed = tests_counts.get('passed')
        pass_pct = aggregate.get('pass_pct')

        physical_validation = actual_accomplishments.get('physical_validation', {}) or {}
        test_analysis = physical_validation.get('test_analysis', {}) or {}

        if tests_total is None:
            tests_total = test_analysis.get('total_tests')
        if tests_failed is None:
            tests_failed = test_analysis.get('failed_tests')
        if tests_error is None:
            tests_error = test_analysis.get('error_tests')
        if tests_skipped is None:
            tests_skipped = test_analysis.get('skipped_tests')
        if tests_passed is None:
            tests_passed = test_analysis.get('passed_tests')

        if pass_pct is None:
            pass_pct = (
                test_analysis.get('pass_rate')
                or test_analysis.get('pass_pct')
                or test_analysis.get('pass_percentage')
            )

        if pass_pct is None and tests_total and tests_passed is not None:
            try:
                pass_pct = (tests_passed / tests_total) * 100
            except ZeroDivisionError:
                pass_pct = None

        modules_expected = aggregate.get('modules_expected')
        modules_seen = aggregate.get('modules_seen')
        skipped_modules = aggregate.get('skipped_modules', []) or []

        exclusions = test_history.get('exclusions', {}) or {}
        exclusions_tests = exclusions.get('tests', []) or []
        exclusions_modules = exclusions.get('modules', []) or []

        phases = {
            'clone': actual_accomplishments.get('repository_cloned', False),
            'build': actual_accomplishments.get('build_success', False),
            'test': actual_accomplishments.get('test_success', False),
        }

        # Get tests_expected_total from actual_accomplishments if available
        tests_expected_total = None
        if actual_accomplishments:
            tests_expected_total = actual_accomplishments.get('tests_expected_total')
        
        status = {
            'overall': verified_status,
            'tests_total': to_int(tests_total),
            'tests_passed': to_int(tests_passed),
            'tests_failed': to_int(tests_failed),
            'tests_errors': to_int(tests_error),
            'tests_skipped': to_int(tests_skipped),
            'pass_pct': pass_pct,
            'modules_expected': to_int(modules_expected),
            'modules_seen': to_int(modules_seen),
            'skipped_modules': skipped_modules,
            'tests_expected_total': to_int(tests_expected_total) if tests_expected_total else None,
        }

        if status['tests_passed'] is None and status['tests_total'] is not None and status['tests_failed'] is not None:
            try:
                status['tests_passed'] = max(status['tests_total'] - status['tests_failed'] - (status['tests_errors'] or 0), 0)
            except TypeError:
                status['tests_passed'] = None

        tests_ok = None
        if pass_pct is not None:
            tests_ok = pass_pct >= 80
        elif status['tests_total'] is not None:
            tests_ok = actual_accomplishments.get('test_success', False)
        status['tests_ok'] = tests_ok

        physical_evidence = {
            'class_files': physical_validation.get('class_files'),
            'jar_files': physical_validation.get('jar_files'),
            'tests_total': status['tests_total'],
            'tests_pass_pct': pass_pct,
        }

        flags = {
            'fail_at_end': test_history.get('flags', {}).get('fail_at_end'),
            'excluded_tests': exclusions_tests,
            'excluded_modules': exclusions_modules,
        }

        snapshot = {
            'status': status,
            'project': {
                'type': project_info.get('type', 'Unknown'),
                'build_system': project_info.get('build_system', 'Unknown'),
            },
            'phases': phases,
            'report_path': f"/workspace/{report_filename}",
            'physical_evidence': physical_evidence,
            'test_history': test_history,
            'per_module': per_module,
            'flags': flags,
            'last_command': test_history.get('last_cmd', {}),
            'failed_tests': test_history.get('failed_tests', []),
        }

        attention = self._evaluate_attention_flags(snapshot)
        snapshot['attention'] = {
            'items': [f"{item['icon']} {item['message']}" for item in attention],
            'raw': attention,
            'ignored_lines': test_history.get('ignored_lines', 0),
        }

        return snapshot

    def _evaluate_attention_flags(self, snapshot: Dict[str, Any]) -> List[Dict[str, str]]:
        """Evaluate needs-attention rules and return ordered severity entries."""

        severity_order = {'BLOCKER': 0, 'WARNING': 1, 'INFO': 2}
        severity_icons = {'BLOCKER': '🔴', 'WARNING': '🟠', 'INFO': '🔵'}
        items: List[Dict[str, str]] = []

        phases = snapshot.get('phases', {})
        status = snapshot.get('status', {})
        test_history = snapshot.get('test_history', {})
        per_module = snapshot.get('per_module', {})
        flags = snapshot.get('flags', {})

        def add(severity: str, message: str):
            items.append({'severity': severity, 'icon': severity_icons[severity], 'message': message})

        # BLOCKER: build failure
        if not phases.get('build', False):
            add('BLOCKER', 'Build failed - compilation or packaging incomplete.')

        # BLOCKER: tests flagged unsuccessful despite telemetry
        if status.get('tests_total') and phases.get('test') is False:
            pass_rate = format_percentage(status.get('pass_pct'))
            add('BLOCKER', f'Tests reported failures (pass rate {pass_rate}).')

        # BLOCKER: build succeeded but no test telemetry captured
        if phases.get('build') and not status.get('tests_total'):
            add('BLOCKER', 'No test reports detected despite successful build.')

        # WARNING: pass rate below threshold (unless already blocker)
        if status.get('pass_pct') is not None and status['pass_pct'] < 80:
            pass_rate = format_percentage(status['pass_pct'])
            add('WARNING', f'Test pass rate below threshold (80%): {pass_rate}.')

        # WARNING: module coverage shortfall
        if status.get('modules_expected') and status.get('modules_seen') is not None:
            if status['modules_seen'] < status['modules_expected']:
                add(
                    'WARNING',
                    f"Module coverage incomplete ({status['modules_seen']}/{status['modules_expected']} tested).",
                )

        # WARNING: skipped modules or exclusions present
        skipped_modules = status.get('skipped_modules') or []
        if skipped_modules:
            skipped_str = truncate_list(skipped_modules)
            add('WARNING', f'Skipped modules detected: {skipped_str}.')

        exclusions_tests = flags.get('excluded_tests') or []
        if exclusions_tests:
            exclusion_str = truncate_list(exclusions_tests)
            add('WARNING', f'Excluded tests patterns applied: {exclusion_str}.')

        # INFO: fail_at_end flag
        if flags.get('fail_at_end'):
            add('INFO', 'fail_at_end enabled (test failures may be deferred).')

        # INFO: modules with low pass percentage
        low_modules = []
        for module, data in per_module.items():
            module_pass = data.get('pass_pct')
            if module_pass is not None and module_pass < 80:
                low_modules.append(f"{module} ({format_percentage(module_pass)})")

        if low_modules:
            low_modules.sort(key=lambda entry: entry)
            add('INFO', f"Modules below 80% pass rate: {truncate_list(low_modules)}.")

        # INFO: ignored telemetry lines
        ignored_lines = test_history.get('ignored_lines', 0)
        if ignored_lines:
            add('INFO', f'Telemetry entries ignored during aggregation: {ignored_lines}.')

        items.sort(key=lambda entry: severity_order[entry['severity']])
        return items

    def _generate_condensed_log_output(
        self,
        verified_status: str,
        report_filename: str,
        actual_accomplishments: dict = None,
        report_snapshot: dict = None,
    ) -> str:
        """Generate condensed output for logs using the shared rendering utility."""
        if report_snapshot:
            snapshot = dict(report_snapshot)
            snapshot['report_path'] = snapshot.get('report_path') or f"/workspace/{report_filename}"
        else:
            project_info = self._get_project_info()
            if actual_accomplishments:
                phases = {
                    'clone': actual_accomplishments.get('repository_cloned', False),
                    'build': actual_accomplishments.get('build_success', False),
                    'test': actual_accomplishments.get('test_success', False),
                }
                physical_validation = actual_accomplishments.get('physical_validation', {})
            else:
                simple_status = self._collect_simple_status_from_tasks()
                phases = {
                    'clone': simple_status.get('clone_success', False),
                    'build': simple_status.get('build_success', False),
                    'test': simple_status.get('test_success', False),
                }
                physical_validation = {}

            evidence = {}
            if physical_validation:
                evidence['class_files'] = physical_validation.get('class_files')
                evidence['jar_files'] = physical_validation.get('jar_files')
                if 'test_analysis' in physical_validation:
                    test_data = physical_validation['test_analysis']
                    evidence['tests_total'] = test_data.get('total_tests')
                    evidence['tests_pass_pct'] = test_data.get('pass_rate') or test_data.get('pass_pct')
                    if evidence['tests_pass_pct'] is None:
                        total = test_data.get('total_tests')
                        passed = test_data.get('passed_tests')
                        if total:
                            evidence['tests_pass_pct'] = (passed / total) * 100 if passed is not None else None

            snapshot = {
                'status': {'overall': verified_status},
                'project': {
                    'type': project_info.get('type', 'Unknown'),
                    'build_system': project_info.get('build_system', 'Unknown'),
                },
                'phases': phases,
                'report_path': f"/workspace/{report_filename}",
                'physical_evidence': evidence,
                'attention': {'items': []},
            }

        condensed_lines = render_condensed_summary(snapshot).split('\n')

        if not actual_accomplishments and not self.physical_validator:
            condensed_lines.append("[⚠️ WARNING: No physical validator - using task-based inference only]")

        if verified_status == "success":
            condensed_lines.append("💡 Next: Project ready for development/deployment! 🎉")
        elif verified_status == "fail":
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
                        "Use manage_context to check current project state"
                    ]
                }
            
            # Check each task status - CRITICAL: Exclude reporting tasks to avoid logical deadlock
            logger.info(f"Checking {len(trunk_context.todo_list)} tasks for completion status")
            incomplete_tasks = []
            for task in trunk_context.todo_list:
                logger.debug(f"Checking task {task.id}: {task.description} - Status: {task.status.value}")
                if task.status.value != "completed":
                    # CRITICAL FIX: Allow reporting task to be in_progress when calling report tool
                    # This prevents the chicken-and-egg problem where the report tool can't run
                    # until the "generate report" task is complete, but the task can't be completed
                    # without running the report tool.
                    if self._is_reporting_task(task):
                        logger.info(f"✅ Allowing reporting task {task.id} to be in_progress during report generation")
                        continue  # Skip reporting tasks from the prerequisite check
                    
                    logger.debug(f"Task {task.id} is incomplete: {task.status.value}")
                    incomplete_tasks.append({
                        "id": task.id,
                        "description": task.description,
                        "status": task.status.value
                    })
            
            # Log TODO status but don't block report generation
            # Status is determined by build/test results, not TODO completion
            if incomplete_tasks:
                total_tasks = len(trunk_context.todo_list)
                completed_tasks = total_tasks - len(incomplete_tasks)
                completion_percentage = (completed_tasks / total_tasks) * 100 if total_tasks > 0 else 0
                
                logger.info(f"📊 TODO List Status: {completed_tasks}/{total_tasks} tasks complete ({completion_percentage:.1f}%)")
                logger.info(f"📝 {len(incomplete_tasks)} tasks remain incomplete (not blocking report)")
                
                # Log incomplete tasks for visibility
                for task in incomplete_tasks:
                    logger.debug(f"  • Incomplete: {task['id']}: {task['description']} (status: {task['status']})")
            else:
                logger.info("✅ All TODO tasks completed")
            
            # Always allow report generation - status based on build/test results only
            return {"valid": True}
            
        except Exception as e:
            logger.error(f"Failed to validate context prerequisites: {e}")
            # In case of error, allow report generation but log the issue
            return {"valid": True}

    def _estimate_phase_durations(self, execution_metrics: dict, actual_accomplishments: dict):
        """
        Estimate phase durations based on execution history and accomplishments.
        
        Args:
            execution_metrics: Execution metrics dictionary to update
            actual_accomplishments: Physical validation results
        """
        try:
            if not self.execution_history_callback:
                return
            
            history = self.execution_history_callback()
            if not history or len(history) == 0:
                return
            
            # Group actions by likely phase
            clone_actions = []
            build_actions = []
            test_actions = []
            
            for step in history:
                # Handle both object and dict formats
                if hasattr(step, 'step_type') and step.step_type == 'action':
                    tool_name = step.tool_name
                    tool_params = step.tool_params
                    timestamp = step.timestamp
                elif isinstance(step, dict) and step.get('step_type') == 'action':
                    tool_name = step.get('tool_name')
                    tool_params = step.get('tool_params', {})
                    timestamp = step.get('timestamp')
                else:
                    continue
                
                if not timestamp:
                    continue
                
                # Categorize actions by phase
                if tool_name == 'project_setup':
                    clone_actions.append(timestamp)
                elif tool_name in ['maven', 'gradle', 'bash']:
                    command = tool_params.get('command', '').lower()
                    if any(build_cmd in command for build_cmd in ['compile', 'package', 'build', 'install']):
                        build_actions.append(timestamp)
                    elif 'test' in command:
                        test_actions.append(timestamp)
            
            # Calculate phase durations
            from datetime import datetime
            
            def calculate_duration(action_timestamps):
                if len(action_timestamps) < 2:
                    return 0
                try:
                    start_time = datetime.fromisoformat(action_timestamps[0].replace('Z', '+00:00'))
                    end_time = datetime.fromisoformat(action_timestamps[-1].replace('Z', '+00:00'))
                    return (end_time - start_time).total_seconds()
                except:
                    return 0
            
            if clone_actions:
                execution_metrics['phases']['clone']['duration'] = calculate_duration(clone_actions)
                execution_metrics['phases']['analyze']['duration'] = execution_metrics['phases']['clone']['duration']
            
            if build_actions:
                execution_metrics['phases']['build']['duration'] = calculate_duration(build_actions)
            
            if test_actions:
                execution_metrics['phases']['test']['duration'] = calculate_duration(test_actions)
            
            logger.debug(f"Phase durations estimated: Clone={execution_metrics['phases']['clone']['duration']}s, "
                        f"Build={execution_metrics['phases']['build']['duration']}s, "
                        f"Test={execution_metrics['phases']['test']['duration']}s")
                        
        except Exception as e:
            logger.warning(f"Failed to estimate phase durations: {e}")
    
    def _is_reporting_task(self, task) -> bool:
        """
        Determine if a task is related to report generation.
        This prevents logical deadlock where report tool can't run until reporting task is complete.
        """
        reporting_keywords = [
            "report", "completion", "summary", "generate", "final", 
            "document", "conclude", "finish", "wrap"
        ]
        
        task_description = task.description.lower()
        return any(keyword in task_description for keyword in reporting_keywords)

    def _reconcile_status(self, claimed_status: str, evidence_status: str, accomplishments: dict) -> str:
        """
        Reconcile claimed status with evidence-based status.
        Binary logic: success (build passed AND test >80%) or fail
        """
        # Extract core step results
        repository_cloned = accomplishments.get('repository_cloned', False)
        build_success = accomplishments.get('build_success', False)
        
        # Calculate test pass rate if available
        test_pass_rate = 0.0
        if 'physical_validation' in accomplishments and 'test_analysis' in accomplishments['physical_validation']:
            test_data = accomplishments['physical_validation']['test_analysis']
            # Use PhysicalValidator's method if available
            if self.physical_validator:
                test_pass_rate = self.physical_validator.calculate_test_pass_rate(test_data)
            else:
                total_tests = test_data.get('total_tests', 0)
                passed_tests = test_data.get('passed_tests', 0)
                if total_tests > 0:
                    test_pass_rate = (passed_tests / total_tests) * 100
        elif accomplishments.get('test_success', False):
            # If tests marked as success without detailed data, assume high pass rate
            test_pass_rate = 100.0
        
        logger.info(f"🔍 Status reconciliation - Claimed: '{claimed_status}', Evidence: '{evidence_status}'")
        logger.info(f"📊 Core steps - Clone: {repository_cloned}, Build: {build_success}, Test pass rate: {test_pass_rate:.1f}%")
        
        # Update evidence_status to use binary logic
        if evidence_status in ["failed", "partial"]:
            evidence_status = "fail"
        
        # Evidence-based status is authoritative
        if not repository_cloned:
            logger.error("❌ Repository clone failed - cannot proceed")
            return "fail"
        
        if not build_success:
            logger.error("❌ Build failed - compilation issues prevent success")
            return "fail"
        
        # Check test pass rate
        if test_pass_rate > 80:
            logger.info(f"✅ Success confirmed: Build passed, Test pass rate {test_pass_rate:.1f}% > 80%")
            return "success"
        else:
            logger.warning(f"❌ Fail: Test pass rate {test_pass_rate:.1f}% <= 80%")
            return "fail"

    def _collect_execution_metrics(self) -> dict:
        """Collect comprehensive execution metrics from the session."""
        metrics = {
            'total_runtime': 0,
            'start_time': None,
            'end_time': None,
            'total_iterations': 0,
            'max_iterations': 0,
            'iteration_utilization': 0,
            'total_thoughts': 0,
            'total_actions': 0,
            'total_observations': 0,
            'successful_actions': 0,
            'failed_actions': 0,
            'success_rate': 0,
            'tools_used': {},
            'tool_failures': {},
            'thinking_model_calls': 0,
            'action_model_calls': 0,
            'phases': {
                'clone': {'status': False, 'duration': 0},
                'analyze': {'status': False, 'duration': 0},
                'build': {'status': False, 'duration': 0},
                'test': {'status': False, 'duration': 0}
            },
            'error_types': {},
            'repetitive_failures': 0
        }
        
        # Get execution history if available
        if self.execution_history_callback:
            try:
                history = self.execution_history_callback()
                
                if history and len(history) > 0:
                    # Calculate timing
                    first_step = history[0]
                    last_step = history[-1]
                    
                    # Get timestamps (handle both object and dict formats)
                    if hasattr(first_step, 'timestamp'):
                        metrics['start_time'] = first_step.timestamp
                    elif isinstance(first_step, dict):
                        metrics['start_time'] = first_step.get('timestamp')
                    
                    if hasattr(last_step, 'timestamp'):
                        metrics['end_time'] = last_step.timestamp
                    elif isinstance(last_step, dict):
                        metrics['end_time'] = last_step.get('timestamp')
                    
                    # Calculate runtime if we have timestamps
                    if metrics['start_time'] and metrics['end_time']:
                        from datetime import datetime
                        try:
                            start = datetime.fromisoformat(metrics['start_time'].replace('Z', '+00:00'))
                            end = datetime.fromisoformat(metrics['end_time'].replace('Z', '+00:00'))
                            metrics['total_runtime'] = (end - start).total_seconds() / 60  # in minutes
                        except:
                            pass
                    
                    # Count step types
                    for step in history:
                        # Handle both object and dict formats
                        if hasattr(step, 'step_type'):
                            step_type = step.step_type
                            tool_name = step.tool_name
                            tool_result = step.tool_result
                            model_used = step.model_used
                        elif isinstance(step, dict):
                            step_type = step.get('step_type')
                            tool_name = step.get('tool_name')
                            tool_result = step.get('tool_result')
                            model_used = step.get('model_used')
                        else:
                            continue
                        
                        # Count by type
                        if step_type == 'thought':
                            metrics['total_thoughts'] += 1
                            # Check which model was actually used
                            if model_used:
                                if any(thinking_model in str(model_used).lower() for thinking_model in ['o1', 'o4', 'thinking', 'claude-3-opus']):
                                    metrics['thinking_model_calls'] += 1
                                else:
                                    metrics['action_model_calls'] += 1
                            else:
                                # Default to action model if not specified
                                metrics['action_model_calls'] += 1
                        elif step_type == 'action':
                            metrics['total_actions'] += 1
                            # Check which model was actually used for the action
                            if model_used:
                                if any(thinking_model in str(model_used).lower() for thinking_model in ['o1', 'o4', 'thinking', 'claude-3-opus']):
                                    metrics['thinking_model_calls'] += 1
                                else:
                                    metrics['action_model_calls'] += 1
                            else:
                                # Default to action model for actions
                                metrics['action_model_calls'] += 1
                            
                            # Track tool usage
                            if tool_name:
                                metrics['tools_used'][tool_name] = metrics['tools_used'].get(tool_name, 0) + 1
                                
                                # Check success/failure
                                success = False
                                if hasattr(tool_result, 'success'):
                                    success = tool_result.success
                                elif isinstance(tool_result, dict):
                                    success = tool_result.get('success', False)
                                
                                if success:
                                    metrics['successful_actions'] += 1
                                else:
                                    metrics['failed_actions'] += 1
                                    metrics['tool_failures'][tool_name] = metrics['tool_failures'].get(tool_name, 0) + 1
                                    
                                    # Track error types
                                    error_code = None
                                    if hasattr(tool_result, 'error_code'):
                                        error_code = tool_result.error_code
                                    elif isinstance(tool_result, dict):
                                        error_code = tool_result.get('error_code')
                                    
                                    if error_code:
                                        metrics['error_types'][error_code] = metrics['error_types'].get(error_code, 0) + 1
                                        if error_code == 'REPETITIVE_EXECUTION':
                                            metrics['repetitive_failures'] += 1
                                
                                # NOTE: Phase completion tracking is now unified with simple_status
                                # at the end of this method to avoid contradictions
                        
                        elif step_type == 'observation':
                            metrics['total_observations'] += 1
                    
                    # Calculate success rate
                    if metrics['total_actions'] > 0:
                        metrics['success_rate'] = (metrics['successful_actions'] / metrics['total_actions']) * 100
                    
                    # Get iteration count from context manager if available
                    if self.context_manager:
                        try:
                            # This would need to be added to context manager
                            metrics['total_iterations'] = len(history) // 3  # Rough estimate: thought + action + observation
                        except:
                            pass
                            
            except Exception as e:
                logger.warning(f"Failed to collect execution metrics: {e}")
        
        # CRITICAL FIX: Phase status should come from physical validation, not inference
        # This will be updated later with actual physical validation results
        # For now, set defaults that will be overridden
        metrics['phases']['clone']['status'] = False
        metrics['phases']['analyze']['status'] = False
        metrics['phases']['build']['status'] = False
        metrics['phases']['test']['status'] = False
        logger.debug("Phase status will be determined by physical validation")

        test_history = self._load_test_history()
        if test_history:
            metrics['test_history'] = test_history

        return metrics
    
    def _verify_execution_history(self, claimed_status: str, claimed_summary: str) -> tuple[str, dict]:
        """Verify the claimed status using physical validation instead of inference."""
        # Initialize accomplishments
        actual_accomplishments = {
            'repository_cloned': False,
            'build_success': False,
            'test_success': False,
            'tools_successful': [],
            'tools_failed': [],
            'total_actions': 0,
            'successful_actions': 0,
            'detailed_results': {},  # Store detailed results for debugging
            'physical_validation': {}  # Store physical validation results
        }
        
        # CRITICAL: Use physical evidence to determine true status
        # Check actual files instead of inferring from logs or task descriptions
        if self.physical_validator and self.docker_orchestrator:
            try:
                project_info = self._get_project_info()
                project_dir = project_info.get('directory', '/workspace')
                
                # Derive project_name for validator (expects name, not full path)
                project_name_for_validator = None
                try:
                    if project_dir.startswith('/workspace/'):
                        tail = project_dir[len('/workspace/'):].strip('/')
                        # Only use single-segment project name (avoid nested paths)
                        if tail and '/' not in tail:
                            project_name_for_validator = tail
                except Exception:
                    project_name_for_validator = None
                
                # Use PhysicalValidator for comprehensive validation (scoped to project when known)
                validation_result = self.physical_validator.validate_build_artifacts(project_name=project_name_for_validator)
                
                # Use enhanced test parsing with metrics for better accuracy
                test_analysis = self.physical_validator.parse_test_reports_with_metrics(project_dir)
                
                # Also get test validation status for additional insights
                test_status = self.physical_validator.validate_test_status(project_name_for_validator)
                
                # Log test status insights
                if test_status.get('pass_rate', 0) <= 80 and test_status.get('has_test_reports'):
                    logger.warning(f"⚠️ Test pass rate is {test_status['pass_rate']:.1f}% (below 80% threshold)")
                if test_status.get('test_exclusions'):
                    logger.warning(f"⚠️ Detected test exclusions: {', '.join(test_status['test_exclusions'])}")
                
                # Extract results from PhysicalValidator
                # Repository is cloned if artifacts detected OR the project directory exists under /workspace
                actual_accomplishments['repository_cloned'] = (
                    validation_result.get('class_files', 0) > 0 or
                    validation_result.get('jar_files', 0) > 0 or
                    len(validation_result.get('missing_classes', [])) > 0
                )
                # Strengthen with directory existence check
                try:
                    dir_check = self.docker_orchestrator.execute_command(
                        f"test -d {project_dir} && echo EXISTS || echo MISSING"
                    )
                    if 'EXISTS' in (dir_check.get('output') or ''):
                        actual_accomplishments['repository_cloned'] = True
                except Exception as _e:
                    logger.debug(f"Directory existence check failed: {_e}")
                actual_accomplishments['build_success'] = validation_result.get('valid', False)
                
                if test_analysis.get('valid'):
                    actual_accomplishments['test_success'] = test_analysis['test_success']
                    # Initialize physical_validation if not exists
                    if 'physical_validation' not in actual_accomplishments:
                        actual_accomplishments['physical_validation'] = {}
                    actual_accomplishments['physical_validation']['test_analysis'] = {
                        'total_tests': test_analysis['total_tests'],
                        'passed_tests': test_analysis['passed_tests'],
                        'failed_tests': test_analysis['failed_tests'],
                        'error_tests': test_analysis['error_tests'],
                        'skipped_tests': test_analysis['skipped_tests'],
                        'report_files_count': len(test_analysis['report_files']),
                        'test_exclusions': test_analysis.get('test_exclusions', []),
                        'modules_without_tests': test_analysis.get('modules_without_tests', [])
                    }
                    
                    # Log if tests were excluded
                    if test_analysis.get('test_exclusions'):
                        logger.warning(f"⚠️ Test exclusions detected: {', '.join(test_analysis['test_exclusions'])}")
                    
                    if test_analysis['test_success']:
                        logger.info(f"✅ PHYSICAL: Tests passed - {test_analysis['passed_tests']}/{test_analysis['total_tests']} tests successful")
                    else:
                        logger.warning(f"❌ PHYSICAL: Tests failed - {test_analysis['failed_tests']} failures, {test_analysis['error_tests']} errors out of {test_analysis['total_tests']} total")
                else:
                    actual_accomplishments['test_success'] = False
                    logger.info("⚠️ PHYSICAL: No test reports found or parsing failed")
                
                # Store validation results - initialize if not exists
                if 'physical_validation' not in actual_accomplishments:
                    actual_accomplishments['physical_validation'] = {}
                actual_accomplishments['physical_validation'].update({
                    'class_files': validation_result.get('class_files', 0),
                    'jar_files': validation_result.get('jar_files', 0),
                    'recent_compilation': validation_result.get('recent_compilation', False),
                    'missing_classes': len(validation_result.get('missing_classes', []))
                })
                
                # CRITICAL: ENFORCE LOGICAL CONSISTENCY
                if not actual_accomplishments['repository_cloned']:
                    if actual_accomplishments['build_success'] or actual_accomplishments['test_success']:
                        logger.error("🚨 IMPOSSIBLE STATE: Build/test without repository!")
                    actual_accomplishments['build_success'] = False
                    actual_accomplishments['test_success'] = False
                    logger.info("⚠️ CONSISTENCY: No clone → no build/test")
                elif not actual_accomplishments['build_success']:
                    if actual_accomplishments['test_success']:
                        logger.error("🚨 IMPOSSIBLE STATE: Test without build!")
                    actual_accomplishments['test_success'] = False
                    logger.info("⚠️ CONSISTENCY: No build → no test")
                
                logger.info(f"📊 PHYSICAL TRUTH: Clone={actual_accomplishments['repository_cloned']}, "
                           f"Build={actual_accomplishments['build_success']}, "
                           f"Test={actual_accomplishments['test_success']}")
                
            except Exception as e:
                logger.warning(f"Physical validation error: {e}")
        elif self.docker_orchestrator:
            # Fallback to simplified physical checks if no PhysicalValidator injected
            try:
                project_info = self._get_project_info()
                project_dir = project_info.get('directory', '/workspace')
                
                # Simplified repository check
                source_count = self.docker_orchestrator.execute_command(
                    f"find {project_dir} -type f \\( -name '*.java' -o -name '*.py' -o -name '*.js' -o -name '*.ts' -o -name '*.cpp' -o -name '*.c' \\) 2>/dev/null | head -100 | wc -l"
                )
                source_files = int(source_count.get('output', '0').strip())
                actual_accomplishments['repository_cloned'] = source_files > 0
                
                # Simplified build check
                class_count_result = self.docker_orchestrator.execute_command(
                    f"find {project_dir} -name '*.class' -type f 2>/dev/null | wc -l"
                )
                class_count = int(class_count_result.get('output', '0').strip())
                actual_accomplishments['build_success'] = class_count > 0
                
                # Simplified test check - conservative approach
                test_reports = self.docker_orchestrator.execute_command(
                    f"find {project_dir} -type f \\( -path '*/surefire-reports/*.xml' -o -path '*/test-results/*.xml' \\) 2>/dev/null | wc -l"
                )
                test_report_count = int(test_reports.get('output', '0').strip())
                actual_accomplishments['test_success'] = False  # Conservative without parsing
                
                logger.info(f"📊 FALLBACK PHYSICAL CHECK: Clone={actual_accomplishments['repository_cloned']}, "
                           f"Build={actual_accomplishments['build_success']}, Test={actual_accomplishments['test_success']}")
                
            except Exception as e:
                logger.warning(f"Fallback physical validation error: {e}")
        
        # Fallback to old methods if physical validation unavailable
        if not actual_accomplishments.get('physical_validation'):
            # Method 1: Check current session execution history
            if self.execution_history_callback:
                try:
                    # Get execution history from callback
                    history = self.execution_history_callback()
                    self._analyze_execution_steps(history, actual_accomplishments)
                except Exception as e:
                    logger.warning(f"Failed to get execution history from callback: {e}")
            
            # Method 2: Check project context for completed tasks
            if self.context_manager:
                try:
                    trunk_context = self.context_manager.load_trunk_context()
                    if trunk_context:
                        self._analyze_completed_tasks(trunk_context, actual_accomplishments)
                        logger.debug(f"📊 Analyzed {len(trunk_context.todo_list)} tasks from project context")
                except Exception as e:
                    logger.warning(f"Failed to get project context: {e}")
        
        # CRITICAL FIX: Enforce logical consistency
        # If build failed, tests cannot have succeeded
        if not actual_accomplishments['build_success']:
            actual_accomplishments['test_success'] = False
            actual_accomplishments['test_status'] = 'not_run'
            logger.info("⚠️ Build failed - marking tests as not run (impossible to test without successful build)")
        
        # Determine actual status based on accomplishments
        actual_status = self._determine_actual_status(actual_accomplishments)
        
        # Smart status reconciliation
        if actual_status != claimed_status:
            logger.warning(f"🔍 Status verification: Claimed '{claimed_status}' but evidence suggests '{actual_status}'")
            logger.info(f"🔍 Actual accomplishments: {actual_accomplishments}")
            
            # Reconcile the status - prioritize physical evidence
            if actual_accomplishments.get('physical_validation'):
                logger.info("Using physical validation as primary source of truth")
                return actual_status, actual_accomplishments
            
            reconciled_status = self._reconcile_status(claimed_status, actual_status, actual_accomplishments)
            logger.info(f"🤝 Status reconciled: Using '{reconciled_status}' as final status")
            return reconciled_status, actual_accomplishments
        
        return actual_status, actual_accomplishments
    
    def _analyze_execution_steps(self, history: list, actual_accomplishments: dict):
        """Analyze execution steps from current session history."""
        for step in history:
            # Handle ReActStep objects
            if hasattr(step, 'step_type') and step.step_type == 'action' and hasattr(step, 'tool_result'):
                tool_name = step.tool_name
                tool_result = step.tool_result
                tool_params = step.tool_params
            # Handle dict format
            elif isinstance(step, dict) and step.get('step_type') == 'action' and step.get('tool_result'):
                tool_name = step.get('tool_name')
                tool_result = step.get('tool_result')
                tool_params = step.get('tool_params', {})
            else:
                continue
                
            actual_accomplishments['total_actions'] += 1
            
            # Handle both object and dict format for tool_result
            if hasattr(tool_result, 'success'):
                # ToolResult object
                success = tool_result.success
                output = tool_result.output
            elif isinstance(tool_result, dict):
                # Dictionary format
                success = tool_result.get('success', False)
                output = tool_result.get('output', '')
            else:
                # Unknown format, assume failure
                success = False
                output = str(tool_result)
            
            if success:
                actual_accomplishments['successful_actions'] += 1
                actual_accomplishments['tools_successful'].append(tool_name)
                
                # STEP 1: Check for repository clone
                if tool_name == 'project_setup':
                    # project_setup tool success means repository was cloned
                    # Check output for confirmation
                    if 'Repository cloned' in output or 'successfully cloned' in output or 'Directory:' in output:
                        actual_accomplishments['repository_cloned'] = True
                        logger.debug("✅ Repository clone detected as successful via project_setup")
                
                # STEP 2: Check for build success (multiple build systems)
                elif tool_name in ['maven', 'gradle', 'bash']:
                    command = tool_params.get('command', '').lower()
                    
                    # Maven build detection - support multiple patterns
                    if tool_name == 'maven':
                        # Check for explicit build commands OR generic maven success
                        is_build_command = any(cmd in command for cmd in ['compile', 'package', 'install']) or command == ''
                        has_build_success = any(pattern in output for pattern in ['BUILD SUCCESS', 'Maven build completed successfully', 'build completed successfully'])
                        
                        if is_build_command and has_build_success:
                            actual_accomplishments['build_success'] = True
                            logger.debug(f"✅ Maven build success detected: {command or 'default goal'}")
                    
                    # Gradle build detection - ENHANCED to check for failures
                    elif tool_name == 'gradle' and any(cmd in command for cmd in ['build', 'compile', 'assemble']):
                        # Check for BUILD FAILED first to avoid false positives
                        if 'BUILD FAILED' in output:
                            actual_accomplishments['build_failed'] = True
                            # Count compilation errors if present
                            import re
                            error_count = len(re.findall(r'unmappable character|error:', output, re.IGNORECASE))
                            if error_count > 0:
                                actual_accomplishments['compilation_errors'] = error_count
                                logger.error(f"❌ Gradle build FAILED with {error_count} compilation errors: {command}")
                            else:
                                logger.error(f"❌ Gradle build FAILED: {command}")
                        elif 'BUILD SUCCESSFUL' in output:
                            actual_accomplishments['build_success'] = True
                            logger.info(f"✅ Gradle build success detected: {command}")
                    
                    # Generic build via bash (npm, make, etc.)
                    elif tool_name == 'bash':
                        if any(build_cmd in command for build_cmd in ['npm run build', 'make', 'cargo build', 'go build']):
                            # Check exit code or common success indicators
                            if 'error' not in output.lower() and 'failed' not in output.lower():
                                actual_accomplishments['build_success'] = True
                                logger.info(f"✅ Build success detected via bash: {command}")
                
                # STEP 3: Check for test success (multiple test frameworks)
                if tool_name in ['maven', 'gradle', 'bash']:
                    command = tool_params.get('command', '').lower()
                    
                    # Maven test detection - enhanced patterns
                    if tool_name == 'maven':
                        # Check for test commands OR generic maven success with test results
                        is_test_command = 'test' in command or command == ''
                        has_test_success = any(pattern in output for pattern in ['BUILD SUCCESS', 'Maven build completed successfully', 'build completed successfully'])
                        has_test_results = 'Tests run:' in output or 'tests run' in output.lower()
                        
                        if is_test_command and has_test_success and has_test_results:
                            # Parse test results - support multiple formats
                            import re
                            test_patterns = [
                                r'Tests run: (\d+), Failures: (\d+), Errors: (\d+)',  # Standard format
                                r'Tests: (\d+) run, (\d+) failures, (\d+) errors'      # Alternative format
                            ]
                            
                            for pattern in test_patterns:
                                test_match = re.search(pattern, output)
                                if test_match:
                                    if len(test_match.groups()) == 3:
                                        total_tests = int(test_match.group(1))
                                        failures = int(test_match.group(2))
                                        errors = int(test_match.group(3))
                                    else:
                                        # Handle different group arrangements
                                        total_tests = int(test_match.group(1))
                                        failures = int(test_match.group(2))
                                        errors = int(test_match.group(3))
                                    
                                    if failures == 0 and errors == 0:
                                        actual_accomplishments['test_success'] = True
                                        logger.info(f"✅ Maven tests passed: {total_tests} tests, 0 failures")
                                    else:
                                        logger.warning(f"⚠️ Maven tests had issues: {failures} failures, {errors} errors")
                                    break
                    
                    # Gradle test detection - ENHANCED to check for failures and extract stats
                    elif tool_name == 'gradle' and 'test' in command:
                        # Check for BUILD FAILED first
                        if 'BUILD FAILED' in output:
                            actual_accomplishments['test_failed'] = True
                            # Extract test statistics if available
                            import re
                            test_match = re.search(r'(\d+)\s+tests?\s+completed.*?(\d+)\s+failed', output, re.IGNORECASE)
                            if test_match:
                                total_tests = int(test_match.group(1))
                                failed_tests = int(test_match.group(2))
                                actual_accomplishments['test_stats'] = {
                                    'total': total_tests,
                                    'failed': failed_tests,
                                    'passed': total_tests - failed_tests
                                }
                                logger.error(f"❌ Gradle tests FAILED: {failed_tests}/{total_tests} tests failed")
                            else:
                                logger.error(f"❌ Gradle tests FAILED")
                        elif 'BUILD SUCCESSFUL' in output:
                            # Also check for specific test failures even if build reports success
                            if 'test failed' in output.lower() or 'tests failed' in output.lower():
                                actual_accomplishments['test_failed'] = True
                                logger.warning(f"⚠️ Gradle build successful but some tests failed")
                            else:
                                actual_accomplishments['test_success'] = True
                                logger.info(f"✅ Gradle tests passed")
                    
                    # Generic test via bash (npm test, pytest, etc.)
                    elif tool_name == 'bash' and any(test_cmd in command for test_cmd in ['npm test', 'pytest', 'go test', 'cargo test']):
                        if 'failed' not in output.lower() and 'error' not in output.lower():
                            actual_accomplishments['test_success'] = True
                            logger.info(f"✅ Tests passed via bash: {command}")
                
                # Store detailed results for debugging
                actual_accomplishments['detailed_results'][f"{tool_name}_{actual_accomplishments['total_actions']}"] = {
                    'tool': tool_name,
                    'command': tool_params.get('command', ''),
                    'success': success,
                    'output_snippet': output[:200] if output else ""
                }
                
            else:
                actual_accomplishments['tools_failed'].append(tool_name)
    
    def _analyze_completed_tasks(self, trunk_context, actual_accomplishments: dict):
        """Analyze completed tasks from project context to determine core accomplishments."""
        if not trunk_context or not trunk_context.todo_list:
            return
        
        for task in trunk_context.todo_list:
            if task.status.value == "completed":
                task_desc = task.description.lower()
                
                # Check for project analyzer results with test estimation
                if 'project_analyzer' in task_desc or 'analyze project' in task_desc:
                    if task.key_results:
                        # Try to extract tests_expected_total from key_results
                        import re
                        match = re.search(r'tests_expected_total[\s=:]+(\d+)', task.key_results)
                        if match:
                            actual_accomplishments['tests_expected_total'] = int(match.group(1))
                            logger.info(f"Found tests_expected_total: {match.group(1)} from project analyzer")
                key_results = getattr(task, 'key_results', '') or ''
                
                # Check for repository clone
                if any(keyword in task_desc for keyword in ['clone', 'repository', 'setup']):
                    # More flexible detection for repository clone
                    clone_indicators = ['repo_dir=', 'repository', 'cloned', '/workspace/', 'project_type=', 'directory']
                    if any(indicator in key_results.lower() for indicator in clone_indicators):
                        actual_accomplishments['repository_cloned'] = True
                        logger.debug(f"✅ Repository clone confirmed from task: {task.description[:50]}...")
                
                # Check for build success
                if any(keyword in task_desc for keyword in ['compile', 'build', 'maven']):
                    build_indicators = [
                        'compile_success=true', 'build_success=true', 'build_status=success',
                        'modules_compiled:', 'output_directory', 'target/', 'compiled', 'build'
                    ]
                    if any(indicator in key_results.lower() for indicator in build_indicators):
                        actual_accomplishments['build_success'] = True
                        logger.debug(f"✅ Build success confirmed from task: {task.description[:50]}...")
                
                # Check for test success
                if any(keyword in task_desc for keyword in ['test', 'run test']):
                    test_indicators = [
                        'tests passed', 'test reports', 'all tests', 'surefire-reports',
                        'tests_passed=true', 'tests_passed": true', 
                        'test_status=success',  # FIXED: Added missing key indicator
                        'test_command=', 'exit_code=0', 'mvn.*success', 'test.*success'
                    ]
                    # FIXED: Removed 'build_success=true' from test indicators as it's misleading
                    if any(indicator in key_results.lower() for indicator in test_indicators):
                        actual_accomplishments['test_success'] = True
                        logger.debug(f"✅ Test success confirmed from task: {task.description[:50]}...")
                
                # Count successful completion
                actual_accomplishments['total_actions'] += 1
                actual_accomplishments['successful_actions'] += 1

    def _verify_physical_artifacts(self, accomplishments: dict) -> bool:
        """
        Verify that physical build artifacts actually exist in the container.
        This prevents false positives where build claims success but no .class or .jar files exist.
        """
        if not self.docker_orchestrator:
            return True  # Can't verify without orchestrator, assume success
        
        try:
            project_info = accomplishments.get('project_info', {}) or self._get_project_info()
            project_dir = project_info.get('directory', '/workspace')
            build_system = project_info.get('build_system', '').lower()
            
            # Different artifact patterns based on build system
            artifact_checks = []
            
            if 'maven' in build_system:
                # Check for .class files in target/classes
                artifact_checks.extend([
                    f"find {project_dir} -path '*/target/classes/*.class' -type f | head -5",
                    f"find {project_dir} -name '*.jar' -path '*/target/*' -type f | head -5"
                ])
            elif 'gradle' in build_system:
                # Check for .class files in build/classes
                artifact_checks.extend([
                    f"find {project_dir} -path '*/build/classes/*/*.class' -type f | head -5",
                    f"find {project_dir} -name '*.jar' -path '*/build/*' -type f | head -5"
                ])
            else:
                # Generic checks for any build system
                artifact_checks.extend([
                    f"find {project_dir} -name '*.class' -type f | head -5",
                    f"find {project_dir} -name '*.jar' -type f | head -5",
                    f"find {project_dir} -name '*.war' -type f | head -5"
                ])
            
            # Execute checks
            artifacts_found = False
            for check_cmd in artifact_checks:
                result = self.docker_orchestrator.execute_command(check_cmd)
                if result.get('exit_code') == 0 and result.get('output', '').strip():
                    artifacts_found = True
                    logger.debug(f"✅ Found build artifacts: {result['output'][:100]}...")
                    break
            
            if not artifacts_found:
                logger.warning("⚠️ No compiled artifacts (.class/.jar files) found despite build success claim")
                # Store details for reporting
                accomplishments['missing_artifacts'] = True
            
            return artifacts_found
            
        except Exception as e:
            logger.warning(f"Failed to verify physical artifacts: {e}")
            return True  # Don't fail on verification errors
    
    def _determine_actual_status(self, accomplishments: dict) -> str:
        """
        Determine the actual status based on build and test results.
        Binary status logic (no partial):
        - SUCCESS: Build passed AND test pass rate > 80%
        - FAIL: Build failed OR test report not found OR test pass rate <= 80%
        """
        # Extract the three core indicators
        repository_cloned = accomplishments.get('repository_cloned', False)
        build_success = accomplishments.get('build_success', False) 
        test_success = accomplishments.get('test_success', False)
        
        # Check for explicit build/test failures (higher priority than success flags)
        build_failed = accomplishments.get('build_failed', False)
        test_failed = accomplishments.get('test_failed', False)
        compilation_errors = accomplishments.get('compilation_errors', 0)
        
        logger.debug(f"🔍 Core status check - Clone: {repository_cloned}, Build: {build_success}, Test: {test_success}")
        logger.debug(f"🔍 Failure check - Build Failed: {build_failed}, Test Failed: {test_failed}, Compilation Errors: {compilation_errors}")
        
        # Step 1: Check if repository was cloned
        if not repository_cloned:
            logger.error("❌ Repository clone failed - this is a fundamental failure")
            return "fail"
        
        # Step 2: Check for explicit build failures or compilation errors
        if build_failed or compilation_errors > 0:
            logger.error(f"❌ Build explicitly failed with {compilation_errors} compilation errors")
            return "fail"
        
        # Step 3: Verify physical artifacts if build claims success
        if build_success and self.docker_orchestrator:
            artifacts_exist = self._verify_physical_artifacts(accomplishments)
            if not artifacts_exist:
                logger.warning("⚠️ Build reported success but no compiled artifacts found")
                # Override the success flag
                build_success = False
                accomplishments['build_success'] = False
                accomplishments['artifact_verification_failed'] = True
        
        # Step 4: Check if build completed successfully  
        if not build_success:
            logger.error("❌ Build failed - cannot proceed without successful compilation")
            return "fail"
        
        # Step 5: Calculate test pass rate for final determination
        test_pass_rate = 0.0
        
        # Check if we have physical validation test data
        if 'physical_validation' in accomplishments and 'test_analysis' in accomplishments['physical_validation']:
            test_data = accomplishments['physical_validation']['test_analysis']
            
            # Use PhysicalValidator's method if available for consistency
            if self.physical_validator:
                test_pass_rate = self.physical_validator.calculate_test_pass_rate(test_data)
            else:
                # Fallback to manual calculation
                total_tests = test_data.get('total_tests', 0)
                passed_tests = test_data.get('passed_tests', 0)
                if total_tests > 0:
                    test_pass_rate = (passed_tests / total_tests) * 100
            
            if test_pass_rate == 0 and test_data.get('total_tests', 0) == 0:
                logger.warning("⚠️ No test reports found - treating as 0% pass rate")
                return "fail"
            else:
                logger.info(f"📊 Test pass rate: {test_pass_rate:.1f}% ({test_data.get('passed_tests', 0)}/{test_data.get('total_tests', 0)})")
        elif test_success:
            # If we don't have detailed test data but tests marked as success, check for test_failed flag
            if test_failed:
                logger.warning("❌ Tests marked as failed")
                return "fail"
            # Assume high pass rate if tests succeeded without detailed data
            test_pass_rate = 100.0
            logger.info("✅ Tests marked as successful (assuming 100% pass rate)")
        else:
            logger.warning("⚠️ No test execution detected - treating as 0% pass rate")
            return "fail"
        
        # Final determination based on pass rate threshold
        if test_pass_rate > 80:
            logger.info(f"✅ SUCCESS: Build passed ✓, Test pass rate {test_pass_rate:.1f}% > 80% ✓")
            return "success"
        else:
            logger.warning(f"❌ FAIL: Test pass rate {test_pass_rate:.1f}% <= 80%")
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
        """Generate console-formatted report with simple summary at the top."""
        
        # ENHANCED: Add simple three-phase summary at the top
        simple_status = self._collect_simple_status_from_tasks()
        simple_summary = self._render_simple_summary_top(simple_status)
        
        report_lines = [
            simple_summary,  # NEW: Simple summary first
            "=" * 80,
            "🎯 DETAILED PROJECT SETUP REPORT",
            "=" * 80,
            f"⏰ Generated: {timestamp}",
            f"📊 Status: {status.upper()}",
            "",
        ]
        
        # Add project information
        if project_info:
            report_lines.extend([
                "📂 PROJECT INFORMATION:",
                f"   • Project Directory: {project_info.get('directory', 'Unknown')}",
                f"   • Project Type: {project_info.get('type', 'Unknown')}",
                f"   • Build System: {project_info.get('build_system', 'Unknown')}",
                "",
            ])
        
        # Add summary
        if summary:
            report_lines.extend([
                "📋 SUMMARY:",
                f"   {summary}",
                "",
            ])
        
        # CRITICAL FIX: Use actual TODO list from trunk context instead of hardcoded tasks
        report_lines.extend([
            "✅ TASK COMPLETION STATUS:",
        ])
        
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
        
        # Fallback to technical accomplishments if no TODO list available
        if not todo_list_used:
            logger.info("Using technical accomplishments as fallback for task status")
            
            # Use actual accomplishments if available
            if actual_accomplishments:
                # Environment setup
                if actual_accomplishments.get('environment_setup'):
                    report_lines.append("   • ✅ Docker environment setup")
                else:
                    report_lines.append("   • ❌ Docker environment setup")
                
                # Repository cloning
                if actual_accomplishments.get('repository_cloned'):
                    report_lines.append("   • ✅ Project repository cloning")
                else:
                    report_lines.append("   • ❌ Project repository cloning")
                
                # Project detection
                if actual_accomplishments.get('project_detected'):
                    report_lines.append("   • ✅ Development environment configuration")
                else:
                    report_lines.append("   • ⚠️ Development environment configuration (partial)")
                
                # Compilation status
                if actual_accomplishments.get('maven_compile_success'):
                    report_lines.append("   • ✅ Project compilation")
                else:
                    report_lines.append("   • ❌ Project compilation (failed)")
                
                # Test execution status
                if actual_accomplishments.get('maven_test_success'):
                    report_lines.append("   • ✅ Test execution")
                else:
                    report_lines.append("   • ❌ Test execution (failed)")
            else:
                # Final fallback to old behavior if no accomplishments data
                report_lines.extend([
                    "   • ✅ Docker environment setup",
                    "   • ✅ Project repository cloning",
                    "   • ✅ Development environment configuration",
                ])
                
                # Add build/test status based on overall status
                if status == "success":
                    report_lines.extend([
                        "   • ✅ Project compilation",
                        "   • ✅ Test execution",
                    ])
                elif status == "partial":
                    report_lines.extend([
                        "   • ⚠️ Project compilation (partial)",
                        "   • ⚠️ Test execution (some issues)",
                    ])
                else:
                    report_lines.extend([
                        "   • ❌ Project compilation (failed)",
                        "   • ❌ Test execution (failed)",
                    ])
        
        # Add comprehensive execution metrics
        if execution_metrics:
            report_lines.extend([
                "",
                "📊 EXECUTION METRICS:",
            ])
            
            # Runtime metrics
            if execution_metrics.get('total_runtime'):
                report_lines.append(f"   • Total runtime: {execution_metrics['total_runtime']:.1f} minutes")
            
            # Iteration metrics
            if execution_metrics.get('total_iterations'):
                report_lines.append(f"   • Iterations used: {execution_metrics['total_iterations']}")
            
            # Step breakdown
            report_lines.extend([
                f"   • Total thoughts: {execution_metrics.get('total_thoughts', 0)}",
                f"   • Total actions: {execution_metrics.get('total_actions', 0)}",
                f"   • Total observations: {execution_metrics.get('total_observations', 0)}",
            ])
            
            # Success metrics
            successful = execution_metrics.get('successful_actions', 0)
            failed = execution_metrics.get('failed_actions', 0)
            success_rate = execution_metrics.get('success_rate', 0)
            report_lines.extend([
                f"   • Successful actions: {successful}",
                f"   • Failed actions: {failed}",
                f"   • Success rate: {success_rate:.1f}%",
            ])
            
            # Model usage
            report_lines.extend([
                f"   • Thinking model calls: {execution_metrics.get('thinking_model_calls', 0)}",
                f"   • Action model calls: {execution_metrics.get('action_model_calls', 0)}",
            ])
            
            # Tool usage
            if execution_metrics.get('tools_used'):
                top_tools = sorted(execution_metrics['tools_used'].items(), key=lambda x: x[1], reverse=True)[:5]
                tools_str = ", ".join([f"{tool}({count})" for tool, count in top_tools])
                report_lines.append(f"   • Most used tools: {tools_str}")
            
            # Error patterns
            if execution_metrics.get('repetitive_failures', 0) > 0:
                report_lines.append(f"   • ⚠️ Repetitive failures: {execution_metrics['repetitive_failures']}")
            
            if execution_metrics.get('error_types'):
                top_errors = sorted(execution_metrics['error_types'].items(), key=lambda x: x[1], reverse=True)[:3]
                for error_type, count in top_errors:
                    report_lines.append(f"   • Error type '{error_type}': {count} occurrences")
        
        # Add legacy execution statistics if no metrics available but accomplishments exist
        elif actual_accomplishments:
            total = actual_accomplishments.get('total_actions', 0)
            successful = actual_accomplishments.get('successful_actions', 0)
            if total > 0:
                success_rate = (successful / total) * 100
                report_lines.extend([
                    "",
                    f"📊 EXECUTION STATISTICS:",
                    f"   • Total actions executed: {total}",
                    f"   • Successful actions: {successful}",
                    f"   • Success rate: {success_rate:.1f}%",
                ])
        
        report_lines.append("")
        
        # Add details if provided
        if details:
            report_lines.extend([
                "📝 DETAILS:",
                f"   {details}",
                "",
            ])
        
        # Add next steps based on status
        if status == "success":
            report_lines.extend([
                "🚀 PROJECT READY:",
                "   • The project has been successfully set up and tested",
                "   • All dependencies are installed and configured",
                "   • You can now start development or deployment",
                "",
            ])
        elif status == "partial":
            report_lines.extend([
                "⚠️ PARTIAL SUCCESS:",
                "   • Basic setup completed but some issues remain",
                "   • Review the logs for specific error details",
                "   • Manual intervention may be needed for full functionality",
                "",
            ])
        else:
            report_lines.extend([
                "❌ SETUP ISSUES:",
                "   • Project setup encountered significant problems",
                "   • Check error logs and dependency requirements",
                "   • Manual troubleshooting may be required",
                "",
            ])
        
        report_lines.extend([
            "=" * 80,
            "Task completed. Setup agent finished.",
            "=" * 80,
        ])
        
        return "\n".join(report_lines)

    def _get_project_info(self) -> Dict[str, str]:
        """Get basic project information from the workspace."""
        import re  # FIXED: Move import to top level to avoid scope issues
        info = {}
        
        try:
            if self.docker_orchestrator:
                # First try to get project info from trunk context
                trunk_context = self.context_manager.load_trunk_context() if self.context_manager else None
                
                # FIXED: Try to detect actual project directory from completed tasks
                project_dir = "/workspace"
                if trunk_context and hasattr(trunk_context, 'todo_list'):
                    for task in trunk_context.todo_list:
                        # FIXED: Use object attributes instead of dictionary access
                        if task.status.value == 'completed' and task.key_results:
                            key_results = task.key_results
                            # Look for project directory in key results
                            if 'repo_dir=' in key_results:
                                # Extract repo_dir value
                                match = re.search(r'repo_dir=([^;,.\s]+)', key_results)
                                if match:
                                    project_dir = match.group(1)
                                    break
                            elif 'path=/workspace/' in key_results:
                                # Try to extract project path - more specific pattern
                                match = re.search(r'path=(/workspace/[\w.-]+)', key_results)
                                if match:
                                    project_dir = match.group(1)
                                    break
                            elif 'Directory=' in key_results or 'directory=' in key_results:
                                # Handle 'Directory=/workspace/<name>' style
                                match = re.search(r'[Dd]irectory=([^;,.\s]+)', key_results)
                                if match and match.group(1).startswith('/workspace/'):
                                    project_dir = match.group(1)
                                    break
                            elif 'clone_location' in key_results:
                                # Handle dict-like key results: {'clone_location': '/workspace/<name>', ...}
                                match = re.search(r"clone_location['\"]?\s*[:=]\s*['\"](/workspace/[^'\"\s]+)['\"]", key_results)
                                if match:
                                    project_dir = match.group(1)
                                    break

                # Fallback: if still /workspace, try to probe a likely project directory
                if project_dir == "/workspace":
                    try:
                        probe_cmd = (
                            "(find /workspace -maxdepth 2 -type f -name pom.xml -printf '%h\\n' 2>/dev/null | head -1) || "
                            "(find /workspace -maxdepth 2 -type f -name build.gradle -printf '%h\\n' 2>/dev/null | head -1) || "
                            "(find /workspace -maxdepth 2 -type f -name package.json -printf '%h\\n' 2>/dev/null | head -1) || "
                            "(find /workspace -mindepth 1 -maxdepth 1 -type d ! -name '.setup_agent' -printf '%p\\n' 2>/dev/null | head -1)"
                        )
                        result = self.docker_orchestrator.execute_command(probe_cmd)
                        candidate = result.get('output', '').strip().split('\n')[0]
                        if candidate and candidate.startswith('/workspace/'):
                            project_dir = candidate
                    except Exception:
                        pass
                
                # ENHANCED: First try to get project type from task key_results
                project_type_from_tasks = None
                if trunk_context and hasattr(trunk_context, 'todo_list'):
                    for task in trunk_context.todo_list:
                        if task.status.value == 'completed' and task.key_results:
                            key_results = task.key_results.lower()
                            if 'project_type=maven' in key_results:
                                project_type_from_tasks = ('Maven Java Project', 'Maven')
                                break
                            elif 'project_type=gradle' in key_results:
                                project_type_from_tasks = ('Gradle Java Project', 'Gradle')
                                break
                            elif 'project_type=node' in key_results or 'project_type=npm' in key_results:
                                project_type_from_tasks = ('Node.js Project', 'npm/yarn')
                                break
                            elif 'project_type=python' in key_results:
                                project_type_from_tasks = ('Python Project', 'pip/poetry')
                                break
                
                # Use project type from tasks if found, otherwise check files
                if project_type_from_tasks:
                    info["type"] = project_type_from_tasks[0]
                    info["build_system"] = project_type_from_tasks[1]
                    info["directory"] = project_dir
                    logger.debug(f"✅ Project type detected from task results: {project_type_from_tasks[0]}")
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
        """Generate markdown-formatted report based on actual project context and execution results."""
        
        report_lines = [
            "# 🎯 Project Setup Report",
            "",
            f"**Generated:** {timestamp}",
            f"**Status:** {status.upper()}",
            "",
        ]

        if report_snapshot:
            report_lines.extend(self._render_conclusion_section(report_snapshot))

            attention_section = self._render_attention_section(report_snapshot)
            if attention_section:
                report_lines.extend(attention_section)

            coverage_section = self._render_test_coverage_section(report_snapshot)
            if coverage_section:
                report_lines.extend(coverage_section)

            current_situation_section = self._render_current_situation_section(report_snapshot)
            if current_situation_section:
                report_lines.extend(current_situation_section)

            execution_detail_section = self._render_execution_details_section(report_snapshot)
            if execution_detail_section:
                report_lines.extend(execution_detail_section)

        # Add project information from actual context
        if project_info:
            report_lines.extend([
                "## 📂 Project Information",
                "",
                f"- **Project Directory:** {project_info.get('directory', 'Unknown')}",
                f"- **Project Type:** {project_info.get('type', 'Unknown')}",
                f"- **Build System:** {project_info.get('build_system', 'Unknown')}",
                "",
            ])

        # Add agent's summary - this should be provided by the agent based on actual work done
        if summary:
            report_lines.extend([
                "## 📋 Executive Summary",
                "",
                summary,
                "",
            ])
        
        # Generate task completion status from trunk context
        task_status_section = self._generate_task_status_section(actual_accomplishments)
        if task_status_section:
            report_lines.extend(task_status_section)
        
        # Add execution details - this should be filled by agent analysis
        if details:
            report_lines.extend([
                "## 📝 Additional Details",
                "",
                details,
                "",
            ])
        
        # Generate technical accomplishments from actual results
        tech_section = self._generate_technical_accomplishments_section(actual_accomplishments)
        if tech_section:
            report_lines.extend(tech_section)
        
        # Generate enhanced error reporting section if errors detected
        error_section = self._generate_error_reporting_section(actual_accomplishments)
        if error_section:
            report_lines.extend(error_section)
        
        # Generate execution metrics section
        metrics_section = self._generate_metrics_section(execution_metrics)
        if metrics_section:
            report_lines.extend(metrics_section)
        
        # Generate next steps based on actual status and context
        next_steps_section = self._generate_next_steps_section(status, actual_accomplishments)
        if next_steps_section:
            report_lines.extend(next_steps_section)
        
        report_lines.extend([
            "---",
            "",
            "**Task completed. Setup Agent has finished.**",
            "",
            f"*This report was automatically generated by Setup-Agent at {timestamp}*",
        ])

        return "\n".join(report_lines)

    def _render_conclusion_section(self, snapshot: Dict[str, Any]) -> List[str]:
        status = snapshot.get('status', {})
        phases = snapshot.get('phases', {})
        evidence = snapshot.get('physical_evidence', {})

        build_icon = '✅' if phases.get('build') else '❌'
        class_files = evidence.get('class_files')
        jar_files = evidence.get('jar_files')
        build_details = []
        if class_files is not None:
            build_details.append(f"{class_files} .class files")
        if jar_files is not None:
            build_details.append(f"{jar_files} .jar files")
        build_suffix = (
            f"Artifacts: {', '.join(build_details)}"
            if build_details
            else "No build artifacts detected"
        )

        tests_total = status.get('tests_total')
        tests_passed = status.get('tests_passed')
        tests_failed = status.get('tests_failed')
        tests_errors = status.get('tests_errors')
        tests_skipped = status.get('tests_skipped')
        pass_pct = status.get('pass_pct')

        if tests_total:
            test_icon = '✅' if status.get('tests_ok', False) else '❌'
            test_summary = (
                f"{tests_passed or 0}/{tests_total} passed"
                f", {tests_failed or 0} failed, {tests_errors or 0} errors"
            )
            if tests_skipped:
                test_summary += f", {tests_skipped} skipped"
            test_summary += f" (pass rate {format_percentage(pass_pct)})"
        else:
            test_icon = '⚠️'
            test_summary = "No test telemetry captured"

        modules_expected = status.get('modules_expected')
        modules_seen = status.get('modules_seen')
        if modules_expected:
            coverage_line = f"{modules_seen or 0}/{modules_expected} modules executed"
        elif modules_seen is not None:
            coverage_line = f"Modules observed: {modules_seen}"
        else:
            coverage_line = None

        lines = [
            "## ✅ Conclusion",
            "",
            f"- **Build:** {build_icon} {build_suffix}",
            f"- **Tests:** {test_icon} {test_summary}",
        ]

        if coverage_line:
            lines.append(f"- **Module Coverage:** {coverage_line}")

        lines.append("")
        return lines

    def _render_attention_section(self, snapshot: Dict[str, Any]) -> List[str]:
        attention_raw = snapshot.get('attention', {}).get('raw', [])
        if not attention_raw:
            return [
                "## 🚨 Needs Attention",
                "",
                "- ✅ No outstanding blocking issues detected.",
                "",
            ]

        lines = ["## 🚨 Needs Attention", ""]
        for item in attention_raw[:5]:
            lines.append(f"- {item['icon']} {item['message']}")

        remaining = len(attention_raw) - 5
        if remaining > 0:
            lines.append(f"- ... (+{remaining} more)")
        lines.append("")
        return lines

    def _render_test_coverage_section(self, snapshot: Dict[str, Any]) -> List[str]:
        status = snapshot.get('status', {})
        per_module = snapshot.get('per_module', {}) or {}
        flags = snapshot.get('flags', {})

        lines = ["## 🧪 Test Coverage", ""]

        if status.get('tests_total'):
            lines.append(
                f"- **Total Tests:** {status['tests_total']} (pass rate {format_percentage(status.get('pass_pct'))})"
            )
            lines.append(
                f"- **Failures/Errors:** {status.get('tests_failed', 0)} failed, "
                f"{status.get('tests_errors', 0)} errors"
            )
        else:
            lines.append("- ⚠️ No aggregated test results available.")

        if status.get('modules_expected'):
            lines.append(
                f"- **Module Coverage:** {status.get('modules_seen', 0)}/"
                f"{status['modules_expected']} modules executed"
            )

        skipped_modules = status.get('skipped_modules') or []
        if skipped_modules:
            lines.append(f"- **Skipped Modules:** {truncate_list(skipped_modules)}")

        exclusions_tests = flags.get('excluded_tests') or []
        if exclusions_tests:
            lines.append(f"- **Excluded Tests:** {truncate_list(exclusions_tests)}")

        lines.append("")

        if per_module:
            lines.extend([
                "| Module | Pass % | Pass/Fail/Error/Skip |",
                "|--------|-------:|---------------------|",
            ])

            sorted_modules = sorted(
                per_module.items(),
                key=lambda item: (item[1].get('pass_pct') if item[1].get('pass_pct') is not None else 101.0,
                                  item[0])
            )

            max_rows = 6
            for idx, (module, data) in enumerate(sorted_modules):
                if idx >= max_rows:
                    break
                pass_pct = format_percentage(data.get('pass_pct'))
                summary = (
                    f"{data.get('passed', 0)}/{data.get('failed', 0)}/"
                    f"{data.get('error', 0)}/{data.get('skipped', 0)}"
                )
                lines.append(f"| {module} | {pass_pct} | {summary} |")

            if len(sorted_modules) > max_rows:
                lines.append(f"| ... | ... | +{len(sorted_modules) - max_rows} more modules |")

            lines.append("")

        return lines

    def _render_current_situation_section(self, snapshot: Dict[str, Any]) -> List[str]:
        """
        Render the Current Situation section that summarizes blockers, warnings,
        and compares expected vs executed test counts.
        """
        lines = ["## 📊 Current Situation", ""]

        status = snapshot.get('status', {})
        phases = snapshot.get('phases', {})
        attention_raw = snapshot.get('attention', {}).get('raw', [])

        tests_expected = status.get('tests_expected_total')
        tests_total = status.get('tests_total')
        tests_failed = status.get('tests_failed', 0) or 0
        tests_errors = status.get('tests_errors', 0) or 0
        tests_skipped = status.get('tests_skipped', 0) or 0
        pass_pct = status.get('pass_pct')

        agent_narrative = snapshot.get('current_situation_narrative')
        if agent_narrative:
            lines.append(agent_narrative)
            lines.append("")

        summary_items: List[str] = []

        phase_labels = [
            ('clone', 'Repository clone'),
            ('build', 'Build phase'),
            ('test', 'Test execution'),
        ]
        for key, label in phase_labels:
            state = phases.get(key)
            if state is True:
                summary_items.append(f"✅ {label} completed")
            elif state is False:
                summary_items.append(f"❌ {label} incomplete")

        if tests_total is not None:
            if tests_expected:
                coverage_pct = (tests_total / tests_expected * 100) if tests_expected else 0
                coverage_icon = "✅" if coverage_pct >= 90 else "⚠️" if coverage_pct >= 50 else "❌"
                summary_items.append(
                    f"{coverage_icon} Tests executed: {tests_total} of ~{tests_expected} ({format_percentage(coverage_pct)})"
                )
            else:
                summary_items.append(f"🧪 Tests executed: {tests_total}")
        else:
            summary_items.append("⚠️ Tests not executed or no telemetry produced")

        if (tests_failed + tests_errors) > 0:
            summary_items.append(
                f"❗ Failures detected: {tests_failed} failed, {tests_errors} errors, {tests_skipped} skipped"
            )
        elif tests_total:
            summary_items.append("✅ All executed tests passed")

        if attention_raw:
            summary_items.append(f"{attention_raw[0]['icon']} {attention_raw[0]['message']}")

        if summary_items:
            lines.append("**Key Points**")
            for item in summary_items:
                lines.append(f"- {item}")
            lines.append("")

        if tests_expected is not None or tests_total is not None:
            lines.extend([
                "### Test Execution Summary",
                "",
                "| Metric | Value | Status |",
                "|--------|-------|--------|",
            ])

            if tests_expected is not None:
                lines.append(f"| Expected Tests | ~{tests_expected} | Estimated via @Test scan |")

            if tests_total is not None:
                status_icon = "✅" if status.get('tests_ok') else "⚠️" if (tests_failed + tests_errors) == 0 else "❌"
                lines.append(f"| Executed Tests | {tests_total} | {status_icon} |")

                if tests_expected is not None and tests_expected > 0:
                    coverage_pct = (tests_total / tests_expected * 100)
                    coverage_icon = "✅" if coverage_pct >= 80 else "⚠️" if coverage_pct >= 50 else "❌"
                    lines.append(f"| Test Coverage | {format_percentage(coverage_pct)} | {coverage_icon} |")

            if status.get('tests_passed') is not None:
                pct_value = pass_pct if pass_pct is not None else 0
                pass_icon = "✅" if pct_value >= 95 else "⚠️" if pct_value >= 80 else "❌"
                lines.append(f"| Pass Rate | {format_percentage(pass_pct)} | {pass_icon} |")

            lines.append("")

        return lines

    def _render_execution_details_section(self, snapshot: Dict[str, Any]) -> List[str]:
        history = snapshot.get('test_history', {}) or {}
        last_cmd = snapshot.get('last_command', {}) or {}
        flags = snapshot.get('flags', {}) or {}
        failed_tests = snapshot.get('failed_tests', []) or []

        lines = ["## 🧰 Execution Details", ""]

        if last_cmd:
            command = last_cmd.get('command') or '<unknown>'
            exit_code = last_cmd.get('exit_code')
            tool = last_cmd.get('tool') or 'maven'
            workdir = last_cmd.get('workdir') or 'N/A'
            lines.append(f"- **Tool:** {tool}")
            lines.append(f"- **Working Directory:** {workdir}")
            lines.append(f"- **Command:** `{command}`")
            if exit_code is not None:
                lines.append(f"- **Exit Code:** {exit_code}")

        if flags.get('fail_at_end'):
            lines.append("- **Flag:** `fail_at_end=True`")

        exclusions_tests = flags.get('excluded_tests') or []
        if exclusions_tests:
            lines.append(f"- **Excluded Tests:** {truncate_list(exclusions_tests)}")

        exclusions_modules = flags.get('excluded_modules') or []
        if exclusions_modules:
            lines.append(f"- **Excluded Modules:** {truncate_list(exclusions_modules)}")

        ignored_lines = history.get('ignored_lines', 0)
        if ignored_lines:
            lines.append(f"- **Telemetry:** {ignored_lines} lines ignored during aggregation")

        if failed_tests:
            lines.append(f"- **Failed Tests (latest):** {truncate_list(failed_tests)}")

        lines.append("")
        return lines

    def _generate_task_status_section(self, actual_accomplishments: dict = None) -> list:
        """Generate task completion status section based on trunk context."""
        if not self.context_manager:
            return []
        
        try:
            trunk_context = self.context_manager.load_trunk_context()
            if not trunk_context or not trunk_context.todo_list:
                return []
            
            section_lines = [
                "## ✅ Task Completion Status",
                "",
            ]
            
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
                
                section_lines.append(f"- {icon} **{task.description}** - {status_text}")
            
            section_lines.append("")
            return section_lines
            
        except Exception as e:
            logger.warning(f"Failed to generate task status section: {e}")
            return []

    def _generate_technical_accomplishments_section(self, actual_accomplishments: dict = None) -> list:
        """Generate technical accomplishments section based on actual execution results."""
        if not actual_accomplishments:
            return []
        
        section_lines = [
            "## 🔧 Technical Accomplishments",
            "",
        ]
        
        # Repository and project setup
        if actual_accomplishments.get('repository_cloned'):
            section_lines.append("- ✅ **Repository Cloned** - Source code successfully downloaded")
        
        if actual_accomplishments.get('project_detected'):
            section_lines.append("- ✅ **Project Type Detected** - Build system and structure identified")
        
        # Build and compilation
        if actual_accomplishments.get('maven_compile_success'):
            section_lines.append("- ✅ **Compilation Successful** - Project builds without errors")
        elif actual_accomplishments.get('repository_cloned'):
            section_lines.append("- ⚠️ **Compilation Issues** - Build encountered problems")
        
        # Testing
        if actual_accomplishments.get('maven_test_success'):
            section_lines.append("- ✅ **Tests Passed** - All test suites executed successfully")
        elif actual_accomplishments.get('maven_compile_success'):
            section_lines.append("- ⚠️ **Test Issues** - Some tests failed or couldn't run")
        
        # Tool usage summary
        successful_tools = actual_accomplishments.get('tools_successful', [])
        if successful_tools:
            unique_tools = list(set(successful_tools))
            section_lines.append(f"- 🛠️ **Tools Used** - {', '.join(unique_tools)}")
        
        # Success rate
        total_actions = actual_accomplishments.get('total_actions', 0)
        successful_actions = actual_accomplishments.get('successful_actions', 0)
        if total_actions > 0:
            success_rate = (successful_actions / total_actions) * 100
            section_lines.append(f"- 📊 **Success Rate** - {successful_actions}/{total_actions} actions ({success_rate:.1f}%)")
        
        section_lines.append("")
        return section_lines

    def _generate_error_reporting_section(self, actual_accomplishments: dict = None) -> list:
        """Generate enhanced error reporting section for build and test failures."""
        if not actual_accomplishments:
            return []
        
        # Check if there are any errors to report
        has_errors = (
            actual_accomplishments.get('build_failed', False) or
            actual_accomplishments.get('test_failed', False) or
            actual_accomplishments.get('compilation_errors', 0) > 0 or
            actual_accomplishments.get('test_failed', False)
        )
        
        if not has_errors:
            return []
        
        section_lines = [
            "## ⚠️ Error Analysis",
            "",
        ]
        
        # Report compilation errors
        if actual_accomplishments.get('compilation_errors', 0) > 0:
            error_count = actual_accomplishments['compilation_errors']
            section_lines.extend([
                "### 🔴 Compilation Errors",
                "",
                f"**Total Compilation Errors:** {error_count}",
                "",
                "**Common Issues Detected:**",
                "- Character encoding problems (unmappable characters)",
                "- Source files may need UTF-8 encoding",
                "- Consider adding `-Dfile.encoding=UTF-8` to build configuration",
                "",
            ])
        
        # Report build failures
        if actual_accomplishments.get('build_failed', False):
            section_lines.extend([
                "### 🔴 Build Failure",
                "",
                "**Status:** BUILD FAILED",
                "",
                "The build process failed to complete successfully. This typically indicates:",
                "- Compilation errors in source code",
                "- Missing dependencies",
                "- Configuration issues",
                "",
                "**Recommended Actions:**",
                "1. Review compilation errors above",
                "2. Check dependency configurations",
                "3. Verify build tool setup (Maven/Gradle)",
                "",
            ])
        
        # Report test failures with statistics
        if actual_accomplishments.get('test_failed', False):
            section_lines.extend([
                "### 🔴 Test Failures",
                "",
            ])
            
            # Add test statistics if available
            test_stats = actual_accomplishments.get('test_stats', {})
            if test_stats:
                total = test_stats.get('total', 0)
                failed = test_stats.get('failed', 0)
                passed = test_stats.get('passed', total - failed)
                pass_rate = (passed / total * 100) if total > 0 else 0
                
                section_lines.extend([
                    "**Test Statistics:**",
                    f"- Total Tests: {total}",
                    f"- Passed: {passed} ({pass_rate:.1f}%)",
                    f"- Failed: {failed}",
                    "",
                ])
            else:
                section_lines.extend([
                    "**Status:** Tests failed (detailed statistics unavailable)",
                    "",
                ])
            
            section_lines.extend([
                "**Recommended Actions:**",
                "1. Review test failure output for specific failing tests",
                "2. Check test logs in `target/surefire-reports/` (Maven) or `build/test-results/` (Gradle)",
                "3. Run failing tests individually for detailed debugging",
                "4. Verify test environment setup and dependencies",
                "",
            ])
        
        # Report tools that failed
        if actual_accomplishments.get('tools_failed'):
            failed_tools = actual_accomplishments['tools_failed']
            section_lines.extend([
                "### 🔧 Failed Tool Executions",
                "",
                f"The following tools encountered errors: {', '.join(set(failed_tools))}",
                "",
            ])
        
        section_lines.append("")
        return section_lines
    
    def _generate_metrics_section(self, execution_metrics: dict = None) -> list:
        """Generate execution metrics section for the markdown report."""
        if not execution_metrics:
            return []
        
        section_lines = [
            "## 📈 Execution Metrics",
            "",
        ]
        
        # Runtime and iterations
        if execution_metrics.get('total_runtime'):
            section_lines.append(f"**Total Runtime:** {execution_metrics['total_runtime']:.1f} minutes")
        if execution_metrics.get('total_iterations'):
            section_lines.append(f"**Iterations Used:** {execution_metrics['total_iterations']}")
        
        section_lines.append("")
        
        # Step breakdown table
        section_lines.extend([
            "### Step Breakdown",
            "",
            "| Step Type | Count |",
            "|-----------|-------|",
            f"| Thoughts | {execution_metrics.get('total_thoughts', 0)} |",
            f"| Actions | {execution_metrics.get('total_actions', 0)} |",
            f"| Observations | {execution_metrics.get('total_observations', 0)} |",
            "",
        ])
        
        # Success metrics
        section_lines.extend([
            "### Performance Metrics",
            "",
            f"- **Successful Actions:** {execution_metrics.get('successful_actions', 0)}",
            f"- **Failed Actions:** {execution_metrics.get('failed_actions', 0)}",
            f"- **Success Rate:** {execution_metrics.get('success_rate', 0):.1f}%",
            f"- **Thinking Model Calls:** {execution_metrics.get('thinking_model_calls', 0)}",
            f"- **Action Model Calls:** {execution_metrics.get('action_model_calls', 0)}",
            "",
        ])
        
        # Tool usage
        if execution_metrics.get('tools_used'):
            section_lines.extend([
                "### Tool Usage",
                "",
                "| Tool | Calls |",
                "|------|-------|",
            ])
            for tool, count in sorted(execution_metrics['tools_used'].items(), key=lambda x: x[1], reverse=True):
                section_lines.append(f"| {tool} | {count} |")
            section_lines.append("")
        
        # Error analysis
        if execution_metrics.get('error_types') or execution_metrics.get('repetitive_failures'):
            section_lines.extend([
                "### Error Analysis",
                "",
            ])
            
            if execution_metrics.get('repetitive_failures', 0) > 0:
                section_lines.append(f"- ⚠️ **Repetitive Failures:** {execution_metrics['repetitive_failures']}")
            
            if execution_metrics.get('error_types'):
                section_lines.extend([
                    "",
                    "**Error Types:**",
                    "",
                ])
                for error_type, count in sorted(execution_metrics['error_types'].items(), key=lambda x: x[1], reverse=True):
                    section_lines.append(f"- `{error_type}`: {count} occurrences")
            
            section_lines.append("")
        
        # Phase completion
        if execution_metrics.get('phases'):
            section_lines.extend([
                "### Phase Completion",
                "",
                "| Phase | Status |",
                "|-------|--------|",
            ])
            for phase, info in execution_metrics['phases'].items():
                status_icon = "✅" if info.get('status') else "❌"
                section_lines.append(f"| {phase.capitalize()} | {status_icon} |")
            section_lines.append("")
        
        return section_lines
    
    def _generate_next_steps_section(self, status: str, actual_accomplishments: dict = None) -> list:
        """Generate next steps section based on actual status and context."""
        section_lines = []
        
        if status == "success":
            section_lines.extend([
                "## 🚀 Project Ready",
                "",
                "- ✅ Project has been successfully set up and tested",
                "- ✅ All dependencies are installed and configured",
                "- ✅ Development environment is ready for use",
                "- 🎯 **Next Steps:** You can now start development or deployment",
                "",
            ])
        elif status == "partial":
            section_lines.extend([
                "## ⚠️ Partial Success",
                "",
                "- ⚠️ Basic setup completed, but some issues remain",
                "- 📋 Review the execution details for specific error information",
                "- 🔧 Manual intervention may be required for full functionality",
            ])
            
            # Add specific recommendations based on what failed
            if actual_accomplishments:
                if not actual_accomplishments.get('maven_compile_success'):
                    section_lines.append("- 🔨 **Recommended:** Check build dependencies and configuration")
                if not actual_accomplishments.get('maven_test_success'):
                    section_lines.append("- 🧪 **Recommended:** Review test failures and fix any issues")
            
            section_lines.append("")
        else:
            section_lines.extend([
                "## ❌ Setup Issues",
                "",
                "- ❌ Project setup encountered significant problems",
                "- 📋 Check error logs and dependency requirements",
                "- 🔧 Manual troubleshooting may be required",
            ])
            
            # Add specific recommendations based on what failed
            if actual_accomplishments:
                if not actual_accomplishments.get('repository_cloned'):
                    section_lines.append("- 📥 **Critical:** Repository clone failed - check URL and access")
                elif not actual_accomplishments.get('project_detected'):
                    section_lines.append("- 🔍 **Critical:** Project type detection failed - verify project structure")
                elif not actual_accomplishments.get('maven_compile_success'):
                    section_lines.append("- 🔨 **Critical:** Build compilation failed - check dependencies")
            
            section_lines.append("")
        
        return section_lines
    
    def _save_markdown_report(self, markdown_content: str, timestamp: str, report_filename: str):
        """Save markdown report to workspace using here-doc for safe handling."""
        
        try:
            if self.docker_orchestrator:
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
                logger.warning("⚠️ Docker orchestrator not available, skipping markdown file creation")
                
        except Exception as e:
            logger.error(f"❌ Error saving markdown report: {e}")
            # Try fallback method on any exception
            if self.docker_orchestrator:
                try:
                    self._save_markdown_report_fallback(markdown_content, f"/workspace/{report_filename}")
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
            encoded_content = base64.b64encode(markdown_content.encode('utf-8')).decode('ascii')
            
            # Write using base64 decode
            command = f"echo '{encoded_content}' | base64 -d > {filepath}"
            result = self.docker_orchestrator.execute_command(command)
            
            if result.get("exit_code") == 0:
                logger.info(f"✅ Markdown report saved via fallback to: {filepath}")
            else:
                logger.error(f"❌ Fallback method failed: {result.get('output', 'Unknown error')}")
                
        except Exception as e:
            logger.error(f"❌ Base64 fallback failed: {e}")

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
            },
            "required": ["action"],
        }

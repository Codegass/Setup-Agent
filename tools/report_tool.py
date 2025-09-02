"""Report tool for generating task summaries and marking completion."""

from typing import Dict, Any, Optional
from datetime import datetime

from loguru import logger

from .base import BaseTool, ToolResult


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
        details: Optional[str] = None
    ) -> ToolResult:
        """
        Generate project setup report and mark completion.
        
        Args:
            action: Action to perform ('generate' for final report)
            summary: Brief summary of what was accomplished
            status: Overall status ('success', 'partial', 'failed')
            details: Additional details about the setup process
        """
        
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
                
                report, verified_status, report_filename, actual_accomplishments = self._generate_comprehensive_report(summary, status, details)
                
                # Mark this as a completion signal for the ReAct engine
                metadata = {
                    "task_completed": True,
                    "completion_signal": True,
                    "status": status,
                    "verified_status": verified_status,  # Include the verified status
                    "timestamp": datetime.now().isoformat(),
                }
                
                # ENHANCED: Provide condensed output for logs to reduce noise
                # Full report is saved to markdown file, logs get summary only
                condensed_output = self._generate_condensed_log_output(verified_status, report_filename, actual_accomplishments)
                
                return ToolResult(
                    success=True,
                    output=condensed_output,
                    metadata=metadata,
                    documentation_links=[],
                    raw_data={"full_report": report}  # Store full report in metadata
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

    def _generate_comprehensive_report(self, summary: str, status: str, details: str) -> tuple[str, str]:
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
        
        # Generate both console and markdown versions with verified information and metrics
        console_report = self._generate_console_report(summary, verified_status, details, timestamp, project_info, actual_accomplishments, execution_metrics)
        markdown_report = self._generate_markdown_report(summary, verified_status, details, timestamp, project_info, actual_accomplishments, execution_metrics)
        
        # Save markdown report to workspace with consistent filename
        self._save_markdown_report(markdown_report, timestamp, report_filename)
        
        return console_report, verified_status, report_filename, actual_accomplishments

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

    def _generate_condensed_log_output(self, verified_status: str, report_filename: str, actual_accomplishments: dict = None) -> str:
        """
        Generate condensed output for logs to reduce noise.
        Only shows essential information instead of the full report.
        """
        project_info = self._get_project_info()
        
        # Create status icons based on actual accomplishments if available, otherwise fallback to task analysis
        if actual_accomplishments:
            # Use physical validation results for accurate status icons
            clone_icon = "✅" if actual_accomplishments.get('repository_cloned', False) else "❌"
            build_icon = "✅" if actual_accomplishments.get('build_success', False) else "❌"
            test_icon = "✅" if actual_accomplishments.get('test_success', False) else "❌"
        else:
            # Fallback to simple task-based status
            simple_status = self._collect_simple_status_from_tasks()
            clone_icon = "✅" if simple_status.get('clone_success', False) else "❌"
            build_icon = "✅" if simple_status.get('build_success', False) else "❌"
            test_icon = "✅" if simple_status.get('test_success', False) else "❌"
        
        # Determine overall status icon
        if verified_status == "success":
            status_icon = "✅"
            status_text = "SUCCESS"
        elif verified_status == "partial":
            status_icon = "⚠️"
            status_text = "PARTIAL"
        else:
            status_icon = "❌"
            status_text = "FAILED"
        
        # Generate condensed summary with consistent filename
        lines = [
            f"🎯 SETUP COMPLETED: {status_icon} {status_text}",
            f"📋 Core Status: {clone_icon} Clone, {build_icon} Build, {test_icon} Test",
            f"📂 Project: {project_info.get('type', 'Unknown')} ({project_info.get('build_system', 'Unknown')})",
            f"📄 Full report saved to: /workspace/{report_filename}"
        ]
        
        # Add physical evidence for failed/partial status
        if verified_status in ["failed", "partial"] and actual_accomplishments:
            physical_validation = actual_accomplishments.get('physical_validation', {})
            if physical_validation:
                class_files = physical_validation.get('class_files', 0)
                jar_files = physical_validation.get('jar_files', 0)
                lines.append(f"[PHYSICAL EVIDENCE: {class_files} .class files, {jar_files} .jar files found]")
            elif not actual_accomplishments.get('build_success', True):
                lines.append("[PHYSICAL EVIDENCE: No .class files found - compilation may have failed]")
        
        # Show warning if no PhysicalValidator was injected
        if not actual_accomplishments and not self.physical_validator:
            lines.append("[⚠️ WARNING: No physical validator - using task-based inference only]")
        
        # Add next steps based on status
        if verified_status == "success":
            lines.append("💡 Next: Project ready for development/deployment! 🎉")
        elif verified_status == "partial":
            lines.append("💡 Next: Review logs and fix remaining issues")
        else:
            lines.append("💡 Next: Check error logs and retry setup")
        
        return "\n".join(lines)

    def _validate_context_prerequisites(self) -> Dict[str, Any]:
        """
        Validate that all prerequisite tasks are completed before generating report.
        This prevents premature report generation when tasks are still in progress.
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
            
            if incomplete_tasks:
                # Check if we should allow partial report generation
                allow_partial = False
                
                # If only 1 task is incomplete and it's currently in progress, allow partial report
                if len(incomplete_tasks) == 1 and incomplete_tasks[0]["status"] == "in_progress":
                    logger.info(f"Allowing partial report generation with 1 in-progress task: {incomplete_tasks[0]['id']}")
                    allow_partial = True
                
                # If more than 80% of tasks are complete, allow partial report with warning
                total_tasks = len(trunk_context.todo_list)
                completed_tasks = total_tasks - len(incomplete_tasks)
                completion_percentage = (completed_tasks / total_tasks) * 100 if total_tasks > 0 else 0
                
                if completion_percentage >= 80:
                    logger.info(f"Allowing partial report generation: {completion_percentage:.1f}% tasks complete")
                    allow_partial = True
                
                if allow_partial:
                    logger.warning(f"Generating partial report with {len(incomplete_tasks)} incomplete tasks")
                    return {
                        "valid": True,
                        "warning": f"Generating partial report: {len(incomplete_tasks)} task(s) incomplete",
                        "incomplete_tasks": incomplete_tasks
                    }
                
                # Format error message with specific incomplete tasks
                task_details = []
                for task in incomplete_tasks:
                    task_details.append(f"  • {task['id']}: {task['description']} (status: {task['status']})")
                
                error_msg = (
                    f"Cannot generate report because {len(incomplete_tasks)} task(s) are not yet complete:\n" +
                    "\n".join(task_details) +
                    "\n\nAll tasks must be completed before generating the final report."
                )
                
                suggestions = [
                    f"Complete the incomplete task(s) first",
                    "Use manage_context to check and complete pending tasks",
                    "If a task is currently in progress, use complete_task() to finish it"
                ]
                
                # Add specific suggestion for current task if in progress
                current_task = self.context_manager.current_task_id
                if current_task:
                    current_task_desc = None
                    for task in incomplete_tasks:
                        if task["id"] == current_task:
                            current_task_desc = task["description"]
                            break
                    if current_task_desc:
                        suggestions.insert(0, f"You are currently working on {current_task}: {current_task_desc}. Complete this task first.")
                
                return {
                    "valid": False,
                    "error": error_msg,
                    "suggestions": suggestions
                }
            
            # All tasks are completed
            logger.info("✅ All prerequisite tasks completed, allowing report generation")
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
        New logic: Based on three core steps (clone, build, test)
        """
        # Extract core step results
        repository_cloned = accomplishments.get('repository_cloned', False)
        build_success = accomplishments.get('build_success', False) 
        test_success = accomplishments.get('test_success', False)
        
        logger.info(f"🔍 Status reconciliation - Claimed: '{claimed_status}', Evidence: '{evidence_status}'")
        logger.info(f"📊 Core steps - Clone: {repository_cloned}, Build: {build_success}, Test: {test_success}")
        
        # Evidence-based status is authoritative for core technical failures
        if evidence_status == "failed":
            if not repository_cloned:
                logger.error("❌ Repository clone failed - cannot proceed")
            elif not build_success:
                logger.error("❌ Build failed - compilation issues prevent success")
            return "failed"
        
        # For partial status, check if user's assessment makes sense
        if evidence_status == "partial":
            if repository_cloned and build_success and not test_success:
                logger.info("✅ Partial status confirmed: clone + build succeeded, tests failed")
                return "partial"
            elif not build_success:
                logger.warning("❌ Build failed but tests may not have run - status remains failed")
                return "failed"
        
        # If evidence shows success, trust it
        if evidence_status == "success":
            logger.info("✅ Success status confirmed by evidence")
            return "success"
        
        # Default to evidence-based assessment
        logger.info(f"🎯 Using evidence-based status: '{evidence_status}'")
        return evidence_status

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
                test_analysis = self.physical_validator.parse_test_reports(project_dir)
                
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
                        'report_files_count': len(test_analysis['report_files'])
                    }
                    
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
                                actual_accomplishments['test_partial'] = True
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
        Determine the actual status based on three core steps: clone, build, test.
        ENHANCED: Now includes physical artifact verification.
        Logic:
        - All success = success
        - Clone or build failed = failed  
        - Only test failed = partial
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
            return "failed"
        
        # Step 2: Check for explicit build failures or compilation errors
        if build_failed or compilation_errors > 0:
            logger.error(f"❌ Build explicitly failed with {compilation_errors} compilation errors")
            return "failed"
        
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
            return "failed"
        
        # Step 5: Check for explicit test failures
        if test_failed:
            test_stats = accomplishments.get('test_stats', {})
            if test_stats:
                failed_count = test_stats.get('failed', 0)
                total_count = test_stats.get('total', 0)
                logger.warning(f"⚠️ Tests explicitly failed - {failed_count}/{total_count} tests failed")
            else:
                logger.warning("⚠️ Tests explicitly failed")
            return "partial"
        
        # Step 6: Check if tests passed
        if not test_success:
            logger.warning("⚠️ Tests not run or incomplete - build succeeded but verification incomplete")
            return "partial"
        
        # All three core steps succeeded
        logger.debug("✅ All core steps succeeded: clone + build + test")
        return "success"

    def _generate_console_report(self, summary: str, status: str, details: str, timestamp: str, project_info: dict, actual_accomplishments: dict = None, execution_metrics: dict = None) -> str:
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
    
    def _generate_markdown_report(self, summary: str, status: str, details: str, timestamp: str, project_info: dict, actual_accomplishments: dict = None, execution_metrics: dict = None) -> str:
        """Generate markdown-formatted report based on actual project context and execution results."""
        
        report_lines = [
            "# 🎯 Project Setup Report",
            "",
            f"**Generated:** {timestamp}",
            f"**Status:** {status.upper()}",
            "",
        ]
        
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
                "## 📝 Execution Details",
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
            actual_accomplishments.get('test_partial', False)
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
        if actual_accomplishments.get('test_failed', False) or actual_accomplishments.get('test_partial', False):
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

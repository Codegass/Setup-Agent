"""Report tool for generating task summaries and marking completion."""

from typing import Dict, Any, Optional
from datetime import datetime

from loguru import logger

from .base import BaseTool, ToolResult


class ReportTool(BaseTool):
    """Tool for generating comprehensive project setup reports and marking task completion."""

    def __init__(self, docker_orchestrator=None, execution_history_callback=None, context_manager=None):
        super().__init__(
            name="report",
            description="Generate comprehensive project setup report and mark task as complete. "
            "Creates both console output and a Markdown file in /workspace. "
            "Use this tool when all main tasks are finished to summarize the work done.",
        )
        self.docker_orchestrator = docker_orchestrator
        self.execution_history_callback = execution_history_callback
        self.context_manager = context_manager

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
                
                report, verified_status = self._generate_comprehensive_report(summary, status, details)
                
                # Mark this as a completion signal for the ReAct engine
                metadata = {
                    "task_completed": True,
                    "completion_signal": True,
                    "status": status,
                    "verified_status": verified_status,  # Include the verified status
                    "timestamp": datetime.now().isoformat(),
                }
                
                return ToolResult(
                    success=True,
                    output=report,
                    metadata=metadata,
                    documentation_links=[]
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
        
        # Get project information if available
        project_info = self._get_project_info()
        
        # Collect execution metrics
        execution_metrics = self._collect_execution_metrics()
        
        # Verify execution history and adjust status/summary if needed
        verified_status, actual_accomplishments = self._verify_execution_history(status, summary)
        
        # Generate both console and markdown versions with verified information and metrics
        console_report = self._generate_console_report(summary, verified_status, details, timestamp, project_info, actual_accomplishments, execution_metrics)
        markdown_report = self._generate_markdown_report(summary, verified_status, details, timestamp, project_info, actual_accomplishments, execution_metrics)
        
        # Save markdown report to workspace
        self._save_markdown_report(markdown_report, timestamp)
        
        return console_report, verified_status

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
            
            logger.info(f"🔍 Simple status: Clone={simple_status['clone_success']}, "
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

    def _validate_context_prerequisites(self) -> Dict[str, Any]:
        """
        Validate that all prerequisite tasks are completed before generating report.
        This prevents premature report generation when tasks are still in progress.
        """
        if not self.context_manager:
            # If no context manager available, allow report generation (backward compatibility)
            logger.warning("No context manager available for prerequisite validation")
            return {"valid": True}
        
        try:
            # Load trunk context to check task statuses
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
            incomplete_tasks = []
            for task in trunk_context.todo_list:
                if task.status.value != "completed":
                    # CRITICAL FIX: Allow reporting task to be in_progress when calling report tool
                    # This prevents the chicken-and-egg problem where the report tool can't run
                    # until the "generate report" task is complete, but the task can't be completed
                    # without running the report tool.
                    if self._is_reporting_task(task):
                        logger.debug(f"Allowing reporting task {task.id} to be in_progress during report generation")
                        continue  # Skip reporting tasks from the prerequisite check
                    
                    incomplete_tasks.append({
                        "id": task.id,
                        "description": task.description,
                        "status": task.status.value
                    })
            
            if incomplete_tasks:
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
        
        # CRITICAL FIX: Unify phase status with simple_status to avoid contradictions
        # Use the same logic as _collect_simple_status_from_tasks for consistency
        simple_status = self._collect_simple_status_from_tasks()
        if simple_status:
            metrics['phases']['clone']['status'] = simple_status.get('clone_success', False)
            metrics['phases']['analyze']['status'] = True  # Always true if we got this far
            metrics['phases']['build']['status'] = simple_status.get('build_success', False)
            metrics['phases']['test']['status'] = simple_status.get('test_success', False)
            logger.info(f"🔧 Phase status unified with simple_status: Clone={metrics['phases']['clone']['status']}, Build={metrics['phases']['build']['status']}, Test={metrics['phases']['test']['status']}")
        
        return metrics
    
    def _verify_execution_history(self, claimed_status: str, claimed_summary: str) -> tuple[str, dict]:
        """Verify the claimed status against actual execution history."""
        # Initialize accomplishments
        actual_accomplishments = {
            'repository_cloned': False,
            'build_success': False,
            'test_success': False,
            'tools_successful': [],
            'tools_failed': [],
            'total_actions': 0,
            'successful_actions': 0,
            'detailed_results': {}  # Store detailed results for debugging
        }
        
        # Method 1: Check current session execution history
        if self.execution_history_callback:
            try:
                # Get execution history from callback
                history = self.execution_history_callback()
                self._analyze_execution_steps(history, actual_accomplishments)
            except Exception as e:
                logger.warning(f"Failed to get execution history from callback: {e}")
        
        # Method 2: Check project context for completed tasks (more comprehensive)
        if self.context_manager:
            try:
                trunk_context = self.context_manager.load_trunk_context()
                if trunk_context:
                    self._analyze_completed_tasks(trunk_context, actual_accomplishments)
                    logger.info(f"📊 Analyzed {len(trunk_context.todo_list)} tasks from project context")
            except Exception as e:
                logger.warning(f"Failed to get project context: {e}")
        
        # Determine actual status based on accomplishments
        actual_status = self._determine_actual_status(actual_accomplishments)
        
        # Smart status reconciliation
        if actual_status != claimed_status:
            logger.warning(f"🔍 Status verification: Claimed '{claimed_status}' but evidence suggests '{actual_status}'")
            logger.info(f"🔍 Actual accomplishments: {actual_accomplishments}")
            
            # Reconcile the status
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
                        logger.info("✅ Repository clone detected as successful via project_setup")
                
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
                            logger.info(f"✅ Maven build success detected: {command or 'default goal'}")
                    
                    # Gradle build detection  
                    elif tool_name == 'gradle' and any(cmd in command for cmd in ['build', 'compile', 'assemble']) and 'BUILD SUCCESSFUL' in output:
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
                    
                    # Gradle test detection
                    elif tool_name == 'gradle' and 'test' in command and 'BUILD SUCCESSFUL' in output:
                        # Look for test results or assume success if build successful
                        if 'failed' not in output.lower():
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
                        logger.info(f"✅ Repository clone confirmed from task: {task.description[:50]}...")
                
                # Check for build success
                if any(keyword in task_desc for keyword in ['compile', 'build', 'maven']):
                    build_indicators = [
                        'compile_success=true', 'build_success=true', 'build_status=success',
                        'modules_compiled:', 'output_directory', 'target/', 'compiled', 'build'
                    ]
                    if any(indicator in key_results.lower() for indicator in build_indicators):
                        actual_accomplishments['build_success'] = True
                        logger.info(f"✅ Build success confirmed from task: {task.description[:50]}...")
                
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
                        logger.info(f"✅ Test success confirmed from task: {task.description[:50]}...")
                
                # Count successful completion
                actual_accomplishments['total_actions'] += 1
                actual_accomplishments['successful_actions'] += 1

    def _determine_actual_status(self, accomplishments: dict) -> str:
        """
        Determine the actual status based on three core steps: clone, build, test.
        Logic:
        - All success = success
        - Clone or build failed = failed  
        - Only test failed = partial
        """
        # Extract the three core indicators
        repository_cloned = accomplishments.get('repository_cloned', False)
        build_success = accomplishments.get('build_success', False) 
        test_success = accomplishments.get('test_success', False)
        
        logger.info(f"🔍 Core status check - Clone: {repository_cloned}, Build: {build_success}, Test: {test_success}")
        
        # Step 1: Check if repository was cloned
        if not repository_cloned:
            logger.error("❌ Repository clone failed - this is a fundamental failure")
            return "failed"
        
        # Step 2: Check if build completed successfully  
        if not build_success:
            logger.error("❌ Build failed - cannot proceed without successful compilation")
            return "failed"
        
        # Step 3: Check if tests passed
        if not test_success:
            logger.warning("⚠️ Tests failed - build succeeded but verification incomplete")
            return "partial"
        
        # All three core steps succeeded
        logger.info("✅ All core steps succeeded: clone + build + test")
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
                                match = re.search(r'repo_dir=([^;,\s]+)', key_results)
                                if match:
                                    project_dir = match.group(1)
                                    break
                            elif 'path=/workspace/' in key_results:
                                # Try to extract project path - more specific pattern
                                match = re.search(r'path=(/workspace/[\w-]+)', key_results)
                                if match:
                                    project_dir = match.group(1)
                                    break
                
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
                    logger.info(f"✅ Project type detected from task results: {project_type_from_tasks[0]}")
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
    
    def _save_markdown_report(self, markdown_content: str, timestamp: str):
        """Save markdown report to workspace."""
        
        try:
            if self.docker_orchestrator:
                # Generate filename with timestamp
                filename = f"setup-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
                filepath = f"/workspace/{filename}"
                
                # Escape content for shell command
                escaped_content = markdown_content.replace("'", "'\"'\"'").replace("\n", "\\n")
                
                # Write the markdown file
                command = f"echo -e '{escaped_content}' > {filepath}"
                result = self.docker_orchestrator.execute_command(command)
                
                if result.get("success"):
                    logger.info(f"✅ Markdown report saved to: {filepath}")
                else:
                    logger.warning(f"⚠️ Failed to save markdown report: {result.get('output', 'Unknown error')}")
            else:
                logger.warning("⚠️ Docker orchestrator not available, skipping markdown file creation")
                
        except Exception as e:
            logger.error(f"❌ Error saving markdown report: {e}")

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

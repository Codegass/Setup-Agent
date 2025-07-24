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
        
        # Verify execution history and adjust status/summary if needed
        verified_status, actual_accomplishments = self._verify_execution_history(status, summary)
        
        # Generate both console and markdown versions with verified information
        console_report = self._generate_console_report(summary, verified_status, details, timestamp, project_info, actual_accomplishments)
        markdown_report = self._generate_markdown_report(summary, verified_status, details, timestamp, project_info, actual_accomplishments)
        
        # Save markdown report to workspace
        self._save_markdown_report(markdown_report, timestamp)
        
        return console_report, verified_status

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
        Intelligently reconcile claimed status with evidence-based status.
        CRITICAL FIX: Base success on actual build/test results, not action success rate.
        """
        # CRITICAL CHANGE: For project setup, evidence-based assessment is AUTHORITATIVE
        # We should never override technical evidence with action success rates
        
        # If evidence shows failed, always use that (something core didn't work)
        if evidence_status == "failed":
            logger.warning("🚨 Evidence shows project setup failed - technical issues detected")
            return "failed"
        
        # CRITICAL: If evidence shows partial (e.g., build succeeded but tests failed)
        # NEVER upgrade to success based on action count - technical results are definitive
        if evidence_status == "partial":
            # For Maven projects, partial means either:
            # 1. Compilation succeeded but tests failed (need to identify failed tests)
            # 2. Project was cloned but build failed
            # 3. Some components worked but critical ones didn't
            
            maven_compile_success = accomplishments.get('maven_compile_success', False)
            maven_test_success = accomplishments.get('maven_test_success', False)
            
            if maven_compile_success and not maven_test_success:
                logger.warning("🔨 Build succeeded but tests failed - this is PARTIAL success, not full success")
                logger.warning("💡 Need to identify which tests failed and why")
            elif not maven_compile_success:
                logger.warning("🚨 Build compilation failed - cannot be considered success")
                
            # NEVER upgrade partial to success based on action success rate
            logger.info(f"🎯 Evidence-based status '{evidence_status}' is definitive for technical assessments")
            return "partial"
        
        # If evidence shows success, trust it (all builds and tests passed)
        if evidence_status == "success":
            logger.info("✅ Evidence confirms genuine success - all builds and tests passed")
            return "success"
        
        # Fallback: if evidence is unclear, prefer the more conservative assessment
        if claimed_status == "success" and evidence_status != "success":
            logger.warning("🤔 Claimed success but evidence unclear - defaulting to partial")
            return "partial"
        
        return evidence_status or claimed_status

    def _verify_execution_history(self, claimed_status: str, claimed_summary: str) -> tuple[str, dict]:
        """Verify the claimed status against actual execution history."""
        if not self.execution_history_callback:
            logger.warning("No execution history available for verification")
            return claimed_status, {}
        
        try:
            # Get execution history from callback
            history = self.execution_history_callback()
            
            # Analyze history for actual accomplishments
            actual_accomplishments = {
                'repository_cloned': False,
                'project_detected': False,
                'maven_compile_success': False,
                'maven_test_success': False,
                'environment_setup': True,  # Assume this is always true if we're running
                'tools_successful': [],
                'tools_failed': [],
                'total_actions': 0,
                'successful_actions': 0,
                'test_details': {} # Initialize for detailed test analysis
            }
            
            # Parse execution steps - handle both dict and object formats
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
                    
                    # Check for specific accomplishments
                    if tool_name == 'project_setup':
                        if tool_params.get('action') == 'clone':
                            actual_accomplishments['repository_cloned'] = True
                        elif tool_params.get('action') == 'detect_project_type':
                            actual_accomplishments['project_detected'] = True
                    
                    elif tool_name == 'maven':
                        command = tool_params.get('command', '').lower()
                        
                        if 'compile' in command and 'BUILD SUCCESS' in output:
                            actual_accomplishments['maven_compile_success'] = True
                        
                        if 'test' in command and 'BUILD SUCCESS' in output and 'Tests run:' in output:
                            # Verify test results
                            import re
                            test_match = re.search(r'Tests run: (\d+), Failures: (\d+), Errors: (\d+)', output)
                            if test_match:
                                total_tests = int(test_match.group(1))
                                failed_tests = int(test_match.group(2))
                                test_errors = int(test_match.group(3))
                                actual_accomplishments['test_details'] = {
                                    'total_tests': total_tests,
                                    'failed_tests': [],
                                    'test_errors': []
                                }
                                if failed_tests > 0:
                                    actual_accomplishments['test_details']['failed_tests'] = [f"Test {i+1}" for i in range(failed_tests)]
                                if test_errors > 0:
                                    actual_accomplishments['test_details']['test_errors'] = [f"Error {i+1}" for i in range(test_errors)]
                                # CRITICAL FIX: Only mark as success if NO failures and NO errors
                                actual_accomplishments['maven_test_success'] = (failed_tests == 0 and test_errors == 0)
                else:
                    actual_accomplishments['tools_failed'].append(tool_name)
            
            # Determine actual status based on accomplishments
            actual_status = self._determine_actual_status(actual_accomplishments)
            
            # CRITICAL FIX: Smart status reconciliation instead of harsh override
            # If agent claims success but evidence suggests otherwise, use smart reconciliation
            if actual_status != claimed_status:
                logger.warning(f"🔍 Status verification: Claimed '{claimed_status}' but evidence suggests '{actual_status}'")
                logger.info(f"🔍 Actual accomplishments: {actual_accomplishments}")
                
                # SMART RECONCILIATION: Consider agent's assessment and context
                reconciled_status = self._reconcile_status(claimed_status, actual_status, actual_accomplishments)
                logger.info(f"🤝 Status reconciled: Using '{reconciled_status}' as final status")
                return reconciled_status, actual_accomplishments
            
            return actual_status, actual_accomplishments
            
        except Exception as e:
            logger.error(f"Failed to verify execution history: {e}")
            return claimed_status, {}

    def _determine_actual_status(self, accomplishments: dict) -> str:
        """
        Determine the actual status based on verifiable accomplishments.
        ENHANCED: Detailed analysis of build and test results with failure attribution.
        """
        # CRITICAL: Maven project success requires BOTH compilation AND all tests passing
        maven_compile_success = accomplishments.get('maven_compile_success', False)
        maven_test_success = accomplishments.get('maven_test_success', False)
        
        # Analyze test results in detail
        test_details = accomplishments.get('test_details', {})
        failed_tests = test_details.get('failed_tests', [])
        test_errors = test_details.get('test_errors', [])
        total_tests = test_details.get('total_tests', 0)
        
        if maven_test_success and maven_compile_success:
            logger.info("✅ Complete success: Compilation and all tests passed")
            return "success"
        elif maven_compile_success and not maven_test_success:
            # Compilation succeeded but tests failed - this is PARTIAL success
            if total_tests > 0:
                failed_count = len(failed_tests) + len(test_errors)
                logger.warning(f"🔨 Partial success: Build OK, but {failed_count}/{total_tests} tests failed")
                
                # Log specific test failures for debugging
                if failed_tests:
                    logger.warning(f"❌ Failed tests: {', '.join(failed_tests[:5])}")  # Show first 5
                if test_errors:
                    logger.warning(f"💥 Test errors: {', '.join(test_errors[:3])}")  # Show first 3
            else:
                logger.warning("🔨 Partial success: Build OK, but test execution had issues")
            return "partial"
        elif not maven_compile_success:
            logger.warning("🚨 Build compilation failed - cannot proceed to testing")
            
            # Check what we DID accomplish
            if accomplishments.get('project_detected'):
                logger.info("📋 Project was detected and analyzed correctly")
                return "partial"  # At least project setup worked
            elif accomplishments.get('repository_cloned'):
                logger.info("📥 Repository was cloned successfully")
                return "partial"  # Basic setup worked
            else:
                logger.error("💥 Complete failure - even basic setup failed")
                return "failed"
        
        # For non-Maven projects or unclear situations
        repository_cloned = accomplishments.get('repository_cloned', False)
        project_detected = accomplishments.get('project_detected', False)
        environment_setup = accomplishments.get('environment_setup', False)
        
        if repository_cloned and project_detected and environment_setup:
            logger.info("📋 Basic project setup completed successfully")
            return "partial"  # Good foundation but no build/test validation
        elif repository_cloned:
            logger.warning("📥 Repository cloned but project analysis incomplete")
            return "partial"
        else:
            # Last resort: check action success rate (only as fallback indicator)
            total = accomplishments.get('total_actions', 0)
            successful = accomplishments.get('successful_actions', 0)
            if total > 0:
                success_rate = successful / total
                logger.warning(f"📊 Fallback assessment based on {success_rate:.1%} action success rate")
                if success_rate >= 0.8:
                    return "partial"  # High success rate suggests partial completion
                else:
                    return "failed"
            
            logger.error("💥 No clear indicators of success")
            return "failed"

    def _generate_console_report(self, summary: str, status: str, details: str, timestamp: str, project_info: dict, actual_accomplishments: dict = None) -> str:
        """Generate console-formatted report."""
        
        report_lines = [
            "=" * 80,
            "🎯 PROJECT SETUP REPORT",
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
        
        # Add execution statistics if available
        if actual_accomplishments:
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
        info = {}
        
        try:
            if self.docker_orchestrator:
                # Check for common project files
                result = self.docker_orchestrator.execute_command("ls -la /workspace")
                if result.get("success"):
                    output = result.get("output", "")
                    
                    # Determine project type based on files
                    if "pom.xml" in output:
                        info["type"] = "Maven Java Project"
                        info["build_system"] = "Maven"
                    elif "package.json" in output:
                        info["type"] = "Node.js Project"
                        info["build_system"] = "npm/yarn"
                    elif "requirements.txt" in output or "pyproject.toml" in output:
                        info["type"] = "Python Project"
                        info["build_system"] = "pip/poetry"
                    else:
                        info["type"] = "Generic Project"
                        info["build_system"] = "Unknown"
                    
                    info["directory"] = "/workspace"
                
        except Exception as e:
            logger.warning(f"Could not gather project info: {e}")
            
        return info
    
    def _generate_markdown_report(self, summary: str, status: str, details: str, timestamp: str, project_info: dict, actual_accomplishments: dict = None) -> str:
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

    def get_usage_example(self) -> str:
        """Get usage examples for the report tool."""
        return """
Report Tool Usage Examples:

IMPORTANT: The summary and details should be based on your actual work and analysis, not generic text.

1. Generate successful completion report:
   report(action="generate", 
          summary="Successfully cloned Apache Commons CLI repository, detected Maven project structure, compiled all modules with zero errors, and executed 127 tests with 100% pass rate. Environment is fully configured and ready for development.",
          status="success",
          details="Cloned repository from https://github.com/apache/commons-cli.git to /workspace/commons-cli. Detected Maven multi-module project with 3 modules. All dependencies resolved successfully. Build completed in 45 seconds. All 127 unit tests passed including integration tests.")

2. Generate partial success report:
   report(action="generate", 
          summary="Repository cloned and project compiled successfully, but 3 test failures prevent complete validation. Core functionality appears working.",
          status="partial", 
          details="Maven compilation succeeded for all modules. However, 3 out of 127 tests failed due to timestamp-related assertions in DateUtilsTest. These appear to be flaky tests and don't affect core CLI parsing functionality.")

3. Generate failure report:
   report(action="generate", 
          summary="Setup failed due to Maven dependency resolution errors. Unable to complete project build.",
          status="failed", 
          details="Repository cloning succeeded, but Maven build failed with 'Could not resolve dependency org.apache.commons:commons-parent:pom:52'. Network connectivity to Maven Central appears to be the issue.")

CRITICAL GUIDELINES:
- Always analyze the actual execution results and provide specific, factual details
- Include concrete numbers (test counts, build times, error counts)
- Mention specific file paths, URLs, and technical details discovered
- Don't use generic phrases - base everything on what actually happened
- The report content will be dynamically enhanced with task status and technical accomplishments

Note: 
- Using this tool marks the task as completed and stops the ReAct loop
- Automatically generates both console output and a Markdown file in /workspace
- Report includes dynamic sections based on trunk context and execution results
""" 
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
            
            # Check each task status
            incomplete_tasks = []
            for task in trunk_context.todo_list:
                if task.status.value != "completed":
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
                'successful_actions': 0
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
                                failures = int(test_match.group(2))
                                errors = int(test_match.group(3))
                                if failures == 0 and errors == 0:
                                    actual_accomplishments['maven_test_success'] = True
                else:
                    actual_accomplishments['tools_failed'].append(tool_name)
            
            # Determine actual status based on accomplishments
            actual_status = self._determine_actual_status(actual_accomplishments)
            
            # Log verification results if there's a discrepancy
            if actual_status != claimed_status:
                logger.warning(f"🔍 Status verification: Claimed '{claimed_status}' but evidence suggests '{actual_status}'")
                logger.info(f"🔍 Actual accomplishments: {actual_accomplishments}")
            
            return actual_status, actual_accomplishments
            
        except Exception as e:
            logger.error(f"Failed to verify execution history: {e}")
            return claimed_status, {}

    def _determine_actual_status(self, accomplishments: dict) -> str:
        """Determine the actual status based on verifiable accomplishments."""
        # For Maven projects, success means compilation and tests both passed
        if accomplishments.get('maven_test_success'):
            return "success"  # Test success implies compilation success too
        elif accomplishments.get('maven_compile_success'):
            return "partial"  # Compilation worked but tests didn't run or failed
        elif accomplishments.get('repository_cloned') and accomplishments.get('project_detected'):
            return "partial"  # Basic setup worked but build failed
        elif accomplishments.get('repository_cloned'):
            return "partial"  # At least repository was cloned
        else:
            # Look at success rate
            total = accomplishments.get('total_actions', 0)
            successful = accomplishments.get('successful_actions', 0)
            if total > 0:
                success_rate = successful / total
                if success_rate >= 0.7:
                    return "partial"
                else:
                    return "failed"
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
        
        # Add status indicators based on actual accomplishments
        report_lines.extend([
            "✅ COMPLETED TASKS:",
        ])
        
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
            
            # Add execution statistics
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
        else:
            # Fallback to old behavior if no accomplishments data
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
        """Generate markdown-formatted report."""
        
        report_lines = [
            "# 🎯 项目设置报告",
            "",
            f"**生成时间:** {timestamp}",
            f"**状态:** {status.upper()}",
            "",
        ]
        
        # Add project information
        if project_info:
            report_lines.extend([
                "## 📂 项目信息",
                "",
                f"- **项目目录:** {project_info.get('directory', 'Unknown')}",
                f"- **项目类型:** {project_info.get('type', 'Unknown')}",
                f"- **构建系统:** {project_info.get('build_system', 'Unknown')}",
                "",
            ])
        
        # Add summary
        if summary:
            report_lines.extend([
                "## 📋 总结",
                "",
                summary,
                "",
            ])
        
        # Add completed tasks
        report_lines.extend([
            "## ✅ 已完成任务",
            "",
            "- ✅ Docker环境设置",
            "- ✅ 项目仓库克隆",
            "- ✅ 开发环境配置",
        ])
        
        # Add build/test status based on overall status
        if status == "success":
            report_lines.extend([
                "- ✅ 项目编译",
                "- ✅ 测试执行",
            ])
        elif status == "partial":
            report_lines.extend([
                "- ⚠️ 项目编译（部分成功）",
                "- ⚠️ 测试执行（存在问题）",
            ])
        else:
            report_lines.extend([
                "- ❌ 项目编译（失败）",
                "- ❌ 测试执行（失败）",
            ])
        
        report_lines.append("")
        
        # Add details if provided
        if details:
            report_lines.extend([
                "## 📝 详细信息",
                "",
                details,
                "",
            ])
        
        # Add next steps based on status
        if status == "success":
            report_lines.extend([
                "## 🚀 项目就绪",
                "",
                "- 项目已成功设置并测试完成",
                "- 所有依赖项已安装并配置",
                "- 现在可以开始开发或部署",
                "",
            ])
        elif status == "partial":
            report_lines.extend([
                "## ⚠️ 部分成功",
                "",
                "- 基本设置已完成，但仍存在一些问题",
                "- 请查看日志以了解具体错误详情",
                "- 可能需要手动干预以实现完整功能",
                "",
            ])
        else:
            report_lines.extend([
                "## ❌ 设置问题",
                "",
                "- 项目设置遇到了重大问题",
                "- 请检查错误日志和依赖项要求",
                "- 可能需要手动故障排除",
                "",
            ])
        
        report_lines.extend([
            "---",
            "",
            "**任务完成。设置代理已结束。**",
            "",
            f"*此报告由 Setup-Agent 于 {timestamp} 自动生成*",
        ])
        
        return "\n".join(report_lines)
    
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

1. Generate successful completion report:
   report(action="generate", summary="Successfully built and tested Maven project", status="success")

2. Generate partial success report:
   report(action="generate", summary="Project setup completed with some test failures", status="partial", details="3 out of 100 tests failed")

3. Generate failure report:
   report(action="generate", summary="Setup failed due to missing dependencies", status="failed", details="Unable to resolve Maven dependencies")

4. Simple completion report:
   report()  # Uses defaults: action="generate", status="success"

Note: 
- Using this tool marks the task as completed and stops the ReAct loop
- Automatically generates both console output and a Markdown file in /workspace
- The MD file is named setup-report-YYYYMMDD-HHMMSS.md for easy identification
""" 
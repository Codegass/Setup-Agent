"""Report tool for generating task summaries and marking completion."""

from typing import Dict, Any, Optional
from datetime import datetime

from loguru import logger

from .base import BaseTool, ToolResult


class ReportTool(BaseTool):
    """Tool for generating comprehensive project setup reports and marking task completion."""

    def __init__(self, docker_orchestrator=None):
        super().__init__(
            name="report",
            description="Generate comprehensive project setup report and mark task as complete. "
            "Use this tool when all main tasks are finished to summarize the work done.",
        )
        self.docker_orchestrator = docker_orchestrator

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
                report = self._generate_comprehensive_report(summary, status, details)
                
                # Mark this as a completion signal for the ReAct engine
                metadata = {
                    "task_completed": True,
                    "completion_signal": True,
                    "status": status,
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
                    error=f"Invalid action '{action}'. Use 'generate' to create report.",
                    suggestions=["Use action='generate' to create the final report"]
                )
                
        except Exception as e:
            logger.error(f"Failed to generate report: {e}")
            return ToolResult(
                success=False,
                error=f"Report generation failed: {str(e)}",
                suggestions=["Check if all required information is available"]
            )

    def _generate_comprehensive_report(self, summary: str, status: str, details: str) -> str:
        """Generate a comprehensive project setup report."""
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Get project information if available
        project_info = self._get_project_info()
        
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
        
        # Add status indicators
        report_lines.extend([
            "✅ COMPLETED TASKS:",
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

Note: Using this tool marks the task as completed and stops the ReAct loop.
""" 
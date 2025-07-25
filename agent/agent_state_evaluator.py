"""Agent State Evaluator for intelligent state analysis and guidance generation."""

from typing import List, Dict, Any
from dataclasses import dataclass
from enum import Enum

from loguru import logger

from .context_manager import ContextManager


class StepType(str, Enum):
    """Types of steps in the ReAct loop (local definition to avoid circular import)."""
    THOUGHT = "thought"
    ACTION = "action"
    OBSERVATION = "observation"
    SYSTEM_GUIDANCE = "system_guidance"


class AgentStatus(str, Enum):
    """Agent operational status."""
    PROCEEDING = "proceeding"  # Normal operation
    STUCK_REPETITION = "stuck_repetition"  # Repeating failed actions
    IDLE_THINKING = "idle_thinking"  # Thinking without action
    TASK_COMPLETE_SIGNAL = "task_complete_signal"  # Technical work seems done
    CONTEXT_SWITCH_NEEDED = "context_switch_needed"  # Been in branch too long
    READY_FOR_REPORT = "ready_for_report"  # All tasks completed
    CONFUSED = "confused"  # Inconsistent state


@dataclass
class AgentStateAnalysis:
    """Result of agent state evaluation."""
    status: AgentStatus
    needs_guidance: bool = False
    guidance_message: str = ""
    guidance_priority: int = 0  # Higher number = more urgent
    is_task_complete: bool = False
    detected_signals: List[str] = None
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.detected_signals is None:
            self.detected_signals = []
        if self.metadata is None:
            self.metadata = {}


class AgentStateEvaluator:
    """
    Centralized state evaluator that consolidates all state checking logic.
    This replaces scattered _check_* methods in ReActEngine.
    """
    
    def __init__(self, context_manager: ContextManager):
        self.context_manager = context_manager
        
        # Completion signal patterns
        self.completion_signals = {
            'repository_cloned': [
                'successfully cloned',
                'cloning into',
                'clone completed',
                'repository cloned'
            ],
            'project_detected': [
                'found pom.xml',
                'maven project detected',
                'package.json found',
                'project type:',
                'build file detected'
            ],
            'dependencies_installed': [
                'dependencies installed',
                'package installation complete',
                'resolved dependencies'
            ],
            'build_success': [
                'BUILD SUCCESS',
                'compilation successful',
                'build completed successfully'
            ],
            'tests_passed': [
                'Tests run:',
                'all tests passed',
                'test suite passed',
                'BUILD SUCCESS'
            ],
            'environment_setup': [
                'environment configured',
                'setup completed',
                'configuration complete'
            ]
        }
        
    def evaluate(
        self, 
        steps: List[Any], 
        current_iteration: int,
        recent_tool_executions: List[Dict],
        steps_since_context_switch: int
    ) -> AgentStateAnalysis:
        """
        Comprehensive evaluation of agent state.
        Consolidates all state checking logic into one place.
        
        Args:
            steps: All ReAct steps taken so far
            current_iteration: Current iteration number
            recent_tool_executions: Recent tool execution history
            steps_since_context_switch: Steps since last context change
            
        Returns:
            AgentStateAnalysis with status and guidance
        """
        
        # Priority order of checks (highest priority first)
        
        # 1. Check for stuck/repetitive execution
        repetition_analysis = self._check_repetitive_execution(recent_tool_executions)
        if repetition_analysis.needs_guidance:
            return repetition_analysis
            
        # 2. Check if task completion opportunity was missed
        if self.context_manager.current_task_id:
            completion_analysis = self._check_task_completion_opportunity(steps)
            if completion_analysis.needs_guidance:
                return completion_analysis
        
        # 3. Check if thinking too much without action
        idle_analysis = self._check_idle_thinking(steps)
        if idle_analysis.needs_guidance:
            return idle_analysis
            
        # 4. Check if context switch is needed
        if self.context_manager.current_task_id:
            context_analysis = self._check_context_switch_needed(steps_since_context_switch)
            if context_analysis.needs_guidance:
                return context_analysis
                
        # 5. Check if ready for final report
        report_analysis = self._check_ready_for_report(steps)
        if report_analysis.needs_guidance:
            return report_analysis
            
        # 6. Check for overall task completion
        if self._is_task_complete(steps):
            return AgentStateAnalysis(
                status=AgentStatus.PROCEEDING,
                is_task_complete=True
            )
        
        # Default: Everything is proceeding normally
        return AgentStateAnalysis(status=AgentStatus.PROCEEDING)
    
    def _check_repetitive_execution(self, recent_tool_executions: List[Dict]) -> AgentStateAnalysis:
        """Check if agent is stuck in repetitive failed execution."""
        if not recent_tool_executions:
            return AgentStateAnalysis(status=AgentStatus.PROCEEDING)
            
        # Count consecutive failures for the same tool
        if len(recent_tool_executions) >= 3:
            last_tool = recent_tool_executions[-1].get("signature", "").split(":")[0]
            consecutive_failures = 0
            
            for exec_record in reversed(recent_tool_executions):
                if exec_record["signature"].startswith(last_tool + ":"):
                    if not exec_record["success"]:
                        consecutive_failures += 1
                    else:
                        break
                else:
                    break
                    
            if consecutive_failures >= 3:
                guidance = (
                    f"🔁 REPETITIVE EXECUTION DETECTED: Tool '{last_tool}' has failed {consecutive_failures} times consecutively.\n\n"
                    f"This indicates the current approach is not working. Consider:\n"
                )
                
                if last_tool == "maven":
                    guidance += (
                        "• Check the project structure and pom.xml location\n"
                        "• Examine build errors in detail with 'mvn -X' for debug output\n"
                        "• Try using bash to manually investigate the issue\n"
                        "• Verify all dependencies are properly configured"
                    )
                elif last_tool == "bash":
                    guidance += (
                        "• Verify the working directory with 'pwd'\n"
                        "• Check command syntax carefully\n"
                        "• Use file_io to examine files before executing commands\n"
                        "• Consider if the environment is properly set up"
                    )
                elif last_tool == "project_setup":
                    guidance += (
                        "• Check if the repository URL is correct\n"
                        "• Verify network connectivity\n"
                        "• Check if the target directory already exists\n"
                        "• Consider using bash to manually clone"
                    )
                else:
                    guidance += (
                        "• Review the error messages carefully\n"
                        "• Try a different approach to achieve the same goal\n"
                        "• Use thinking model to analyze root cause\n"
                        "• Consider using alternative tools"
                    )
                
                return AgentStateAnalysis(
                    status=AgentStatus.STUCK_REPETITION,
                    needs_guidance=True,
                    guidance_message=guidance,
                    guidance_priority=10,  # High priority
                    metadata={"failed_tool": last_tool, "failure_count": consecutive_failures}
                )
                
        return AgentStateAnalysis(status=AgentStatus.PROCEEDING)
    
    def _check_task_completion_opportunity(self, steps: List[Any]) -> AgentStateAnalysis:
        """Check if recent observations indicate task completion opportunity."""
        # Look at recent observations
        recent_observations = []
        for step in reversed(steps[-5:]):  # Last 5 steps
            if hasattr(step, 'step_type') and step.step_type == StepType.OBSERVATION:
                recent_observations.append(step.content)
                
        if not recent_observations:
            return AgentStateAnalysis(status=AgentStatus.PROCEEDING)
            
        # Check for completion signals
        detected_signals = []
        for observation in recent_observations:
            observation_lower = observation.lower()
            for signal_type, patterns in self.completion_signals.items():
                for pattern in patterns:
                    if pattern.lower() in observation_lower:
                        detected_signals.append(signal_type)
                        break
                        
        if detected_signals:
            current_task = self.context_manager.current_task_id
            guidance = (
                f"🚨 TASK COMPLETION SIGNALS DETECTED: {', '.join(set(detected_signals))}\n\n"
                f"Your current task '{current_task}' appears to be technically complete.\n"
                f"CRITICAL: You MUST now call:\n\n"
                f"manage_context(\n"
                f"    action='complete_with_results',\n"
                f"    summary='[Describe what you accomplished]',\n"
                f"    key_results='[Specific results for next task, e.g., file paths, project type]'\n"
                f")\n\n"
                f"This is MANDATORY to prevent 'ghost states' where work is done but not recorded.\n"
                f"DO NOT continue to other work without updating the official task status!"
            )
            
            return AgentStateAnalysis(
                status=AgentStatus.TASK_COMPLETE_SIGNAL,
                needs_guidance=True,
                guidance_message=guidance,
                guidance_priority=9,  # Very high priority
                detected_signals=list(set(detected_signals)),
                metadata={"current_task": current_task}
            )
            
        return AgentStateAnalysis(status=AgentStatus.PROCEEDING)
    
    def _check_idle_thinking(self, steps: List[Any]) -> AgentStateAnalysis:
        """Check if agent is thinking too much without taking action."""
        # Count consecutive thoughts without actions
        consecutive_thoughts = 0
        for step in reversed(steps[-10:]):  # Look at last 10 steps
            if hasattr(step, 'step_type'):
                if step.step_type == StepType.THOUGHT:
                    consecutive_thoughts += 1
                elif step.step_type == StepType.ACTION:
                    break
                    
        if consecutive_thoughts >= 3:
            guidance = (
                f"⚠️ IDLE THINKING DETECTED: You have been thinking for {consecutive_thoughts} steps without action.\n\n"
                f"You MUST take action now. Use one of these tools:\n"
                f"• manage_context - Check current state or complete tasks\n"
                f"• bash - Execute commands in the environment\n"
                f"• maven - Run Maven build commands\n"
                f"• file_io - Read or write files\n"
                f"• project_setup - Clone repositories\n\n"
                f"Stop overthinking and ACT! The tools will handle the execution."
            )
            
            return AgentStateAnalysis(
                status=AgentStatus.IDLE_THINKING,
                needs_guidance=True,
                guidance_message=guidance,
                guidance_priority=7,
                metadata={"consecutive_thoughts": consecutive_thoughts}
            )
            
        return AgentStateAnalysis(status=AgentStatus.PROCEEDING)
    
    def _check_context_switch_needed(self, steps_since_switch: int) -> AgentStateAnalysis:
        """Check if agent has been in branch context too long."""
        threshold = 15  # Reasonable threshold for task completion
        
        if steps_since_switch >= threshold:
            guidance = (
                f"📊 CONTEXT SWITCH REMINDER: You have been working on the current task for {steps_since_switch} steps.\n\n"
                f"Consider if the task is complete:\n"
                f"• If YES: Use manage_context(action='complete_with_results', ...) to finish\n"
                f"• If NO but stuck: Add context about blockers and continue\n"
                f"• If scope creeping: Focus on the specific task requirements\n\n"
                f"Long-running tasks often indicate either completion or blocking issues."
            )
            
            return AgentStateAnalysis(
                status=AgentStatus.CONTEXT_SWITCH_NEEDED,
                needs_guidance=True,
                guidance_message=guidance,
                guidance_priority=5,
                metadata={"steps_in_context": steps_since_switch}
            )
            
        return AgentStateAnalysis(status=AgentStatus.PROCEEDING)
    
    def _check_ready_for_report(self, steps: List[Any]) -> AgentStateAnalysis:
        """Check if all tasks are done and ready for final report."""
        # This requires checking trunk context
        try:
            # Look for recent successful Maven test completion
            recent_success = False
            for step in reversed(steps[-10:]):
                if (hasattr(step, 'step_type') and 
                    step.step_type == StepType.OBSERVATION and 
                    "BUILD SUCCESS" in step.content and 
                    "Tests run:" in step.content):
                    recent_success = True
                    break
                    
            if recent_success and not self.context_manager.current_task_id:
                # We're in trunk context after successful tests
                guidance = (
                    "🎯 PROJECT SETUP APPEARS COMPLETE!\n\n"
                    "Maven tests have passed successfully. If all tasks are done:\n"
                    "• Use the 'report' tool to generate the final project report\n"
                    "• Example: report(summary='Successfully set up and tested Maven project', status='success')\n\n"
                    "This will create a comprehensive report and mark the project as complete."
                )
                
                return AgentStateAnalysis(
                    status=AgentStatus.READY_FOR_REPORT,
                    needs_guidance=True,
                    guidance_message=guidance,
                    guidance_priority=8,
                    metadata={"maven_success": True}
                )
                
        except Exception as e:
            logger.warning(f"Error checking report readiness: {e}")
            
        return AgentStateAnalysis(status=AgentStatus.PROCEEDING)
    
    def _is_task_complete(self, steps: List[Any]) -> bool:
        """Check if the overall task is complete."""
        # Check for successful report generation
        for step in reversed(steps[-5:]):
            if hasattr(step, 'step_type') and step.step_type == StepType.ACTION:
                if (hasattr(step, 'tool_name') and step.tool_name == "report" and
                    hasattr(step, 'tool_result') and step.tool_result and 
                    step.tool_result.success):
                    # Check for completion signal in metadata
                    metadata = getattr(step.tool_result, 'metadata', {})
                    if metadata.get("completion_signal") or metadata.get("task_completed"):
                        logger.info("Task completion detected via report tool")
                        return True
                        
        return False
    
    def get_completion_signals_for_task(self, task_description: str) -> List[str]:
        """Get relevant completion signals based on task description."""
        task_lower = task_description.lower()
        relevant_signals = []
        
        if "clone" in task_lower or "repository" in task_lower:
            relevant_signals.extend(self.completion_signals['repository_cloned'])
        if "detect" in task_lower or "analyze" in task_lower:
            relevant_signals.extend(self.completion_signals['project_detected'])
        if "dependen" in task_lower or "install" in task_lower:
            relevant_signals.extend(self.completion_signals['dependencies_installed'])
        if "build" in task_lower or "compile" in task_lower:
            relevant_signals.extend(self.completion_signals['build_success'])
        if "test" in task_lower:
            relevant_signals.extend(self.completion_signals['tests_passed'])
            
        return relevant_signals 
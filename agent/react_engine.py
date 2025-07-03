"""ReAct Engine for Setup-Agent (SAG)."""

import json
import re
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum

import litellm
from loguru import logger
from pydantic import BaseModel

from config import get_config, create_agent_logger
from tools import BaseTool, ToolResult
from .context_manager import ContextManager, BranchContext, TrunkContext


class StepType(str, Enum):
    """Types of steps in the ReAct loop."""
    THOUGHT = "thought"
    ACTION = "action"
    OBSERVATION = "observation"
    SYSTEM_GUIDANCE = "system_guidance"


class ReActStep(BaseModel):
    """A single step in the ReAct process."""
    step_type: StepType
    content: str
    tool_name: Optional[str] = None
    tool_params: Optional[Dict[str, Any]] = None
    tool_result: Optional[ToolResult] = None
    timestamp: str
    model_used: Optional[str] = None


class ReActEngine:
    """Core ReAct (Reasoning and Acting) engine with dual model support."""
    
    def __init__(self, context_manager: ContextManager, tools: List[BaseTool]):
        self.context_manager = context_manager
        self.tools = {tool.name: tool for tool in tools}
        self.config = get_config()
        
        # ReAct state
        self.steps: List[ReActStep] = []
        self.current_iteration = 0
        self.max_iterations = self.config.max_iterations
        
        # Context switching guidance
        self.steps_since_context_switch = 0
        self.context_switch_threshold = self.config.context_switch_threshold
        
        # Agent logger for detailed traces
        self.agent_logger = create_agent_logger("react_engine")
        
        # Configure LiteLLM
        self._setup_litellm()
        
        logger.info("ReAct Engine initialized with dual model support")
        logger.info(f"Thinking model: {self.config.get_litellm_model_name('thinking')}")
        logger.info(f"Action model: {self.config.get_litellm_model_name('action')}")
    
    def _setup_litellm(self):
        """Setup LiteLLM configuration."""
        # Set LiteLLM to not cache responses for debugging
        litellm.cache = None
        
        # Enable detailed logging for debugging
        if self.config.log_level.value == "DEBUG":
            litellm.set_verbose = True
        
        logger.info("LiteLLM configured")
    
    def run_react_loop(self, initial_prompt: str, max_iterations: Optional[int] = None) -> bool:
        """Run the main ReAct loop."""
        max_iter = max_iterations or self.max_iterations
        
        self.agent_logger.info(f"Starting ReAct loop with max {max_iter} iterations")
        
        # Initialize with the initial prompt
        self.steps = []
        self.current_iteration = 0
        
        # Start with initial thought using thinking model
        current_prompt = self._build_initial_system_prompt() + "\n\n" + initial_prompt
        
        try:
            while self.current_iteration < max_iter:
                self.current_iteration += 1
                self.agent_logger.info(f"ReAct iteration {self.current_iteration}/{max_iter}")
                
                # Determine if this should be a thinking step or action step
                is_thinking_step = self._should_use_thinking_model()
                
                # Get LLM response
                response = self._get_llm_response(current_prompt, is_thinking_step)
                
                if not response:
                    logger.error("Failed to get LLM response")
                    return False
                
                # Parse the response
                parsed_steps = self._parse_llm_response(response, is_thinking_step)
                
                if not parsed_steps:
                    logger.warning("No valid steps parsed from LLM response")
                    continue
                
                # Execute the steps
                success = self._execute_steps(parsed_steps)
                
                # Check for completion
                if self._is_task_complete():
                    self.agent_logger.info("Task completed successfully")
                    return True
                
                # Check for context switching guidance
                self._check_context_switching_guidance()
                
                # Build prompt for next iteration
                current_prompt = self._build_next_prompt()
                
                # Update context step count
                if self.context_manager.current_context:
                    self.context_manager.current_context.increment_step()
                
                self.steps_since_context_switch += 1
            
            logger.warning(f"ReAct loop completed without success after {max_iter} iterations")
            return False
            
        except Exception as e:
            logger.error(f"ReAct loop failed: {e}", exc_info=True)
            return False
    
    def _should_use_thinking_model(self) -> bool:
        """Determine if we should use the thinking model for this step."""
        # Use thinking model for:
        # 1. First step (initial analysis)
        # 2. When we haven't had a thinking step in the last 5 iterations
        # 3. When we encounter errors or complex situations
        
        if self.current_iteration == 1:
            return True
        
        # Check if we've had thinking steps recently
        recent_steps = self.steps[-5:] if len(self.steps) >= 5 else self.steps
        thinking_steps = [s for s in recent_steps if s.model_used and "o1" in s.model_used]
        
        if not thinking_steps:
            return True
        
        # Check for recent errors
        recent_errors = [s for s in recent_steps 
                        if s.step_type == StepType.ACTION and s.tool_result and not s.tool_result.success]
        
        if len(recent_errors) >= 2:
            return True
        
        return False
    
    def _get_llm_response(self, prompt: str, use_thinking_model: bool = False) -> Optional[str]:
        """Get response from the appropriate LLM model."""
        try:
            if use_thinking_model:
                model = self.config.get_litellm_model_name("thinking")
                temperature = self.config.thinking_temperature
                max_tokens = self.config.thinking_max_tokens
                
                # Special handling for o1 models
                if "o1" in model:
                    response = litellm.completion(
                        model=model,
                        messages=[{"role": "user", "content": prompt}],
                        reasoning_effort=self.config.reasoning_effort,
                        max_completion_tokens=max_tokens,
                    )
                else:
                    response = litellm.completion(
                        model=model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
            else:
                model = self.config.get_litellm_model_name("action")
                temperature = self.config.action_temperature
                max_tokens = self.config.action_max_tokens
                
                response = litellm.completion(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            
            content = response.choices[0].message.content
            
            self.agent_logger.info(f"LLM Response from {model}: {len(content)} chars")
            logger.debug(f"Model used: {model}, Response length: {len(content)}")
            
            return content
            
        except Exception as e:
            logger.error(f"LLM request failed: {e}")
            return None
    
    def _build_initial_system_prompt(self) -> str:
        """Build the initial system prompt with context and tool information."""
        
        # Get current context info
        context_info = self.context_manager.get_current_context_info()
        
        prompt = """You are SAG (Setup-Agent), an AI assistant specialized in setting up and configuring software projects.

Your workflow follows the ReAct (Reasoning and Acting) pattern:
1. THOUGHT: Think deeply about what you need to do next
2. ACTION: Use a tool to take action
3. OBSERVATION: Observe the results and plan next steps

CRITICAL CONTEXT MANAGEMENT RULES:
- You work with TWO types of contexts: TRUNK (main) and BRANCH (sub-task)
- TRUNK context: Contains the overall goal and TODO list
- BRANCH context: For focused work on specific tasks
- ALWAYS use manage_context tool to switch between contexts appropriately
- When starting a new task from TODO list, create a branch context
- When completing a task, return to trunk context with a summary

AVAILABLE TOOLS:
"""
        
        # Add tool descriptions
        for tool in self.tools.values():
            prompt += f"\n- {tool.name}: {tool.description}"
        
        prompt += f"""

CURRENT CONTEXT:
Context Type: {context_info.get('context_type', 'unknown')}
Context ID: {context_info.get('context_id', 'unknown')}
"""
        
        if context_info.get('context_type') == 'trunk':
            prompt += f"""
Goal: {context_info.get('goal', 'Not specified')}
Progress: {context_info.get('progress', 'Not available')}
Next Task: {context_info.get('next_task', 'No pending tasks')}
"""
        elif context_info.get('context_type') == 'branch':
            prompt += f"""
Current Task: {context_info.get('task', 'Not specified')}
Current Focus: {context_info.get('focus', 'Not specified')}
"""
        
        prompt += """

RESPONSE FORMAT:
Always respond in this exact format:

THOUGHT: [Your deep reasoning about what to do next, analyze the situation thoroughly]

ACTION: [tool_name]
PARAMETERS: [JSON object with parameters]

Wait for OBSERVATION, then continue with next THOUGHT/ACTION cycle.

IMPORTANT GUIDELINES:
1. Always start with THOUGHT to explain your reasoning
2. Use manage_context tool to switch contexts when appropriate
3. In TRUNK context: analyze TODO list and create branch contexts for tasks
4. In BRANCH context: focus on the specific task, use detailed logging
5. Always provide summaries when returning to trunk context
6. Use bash tool for system operations, file_io for file operations
7. Use web_search when you encounter unknown errors or need documentation
8. Be methodical and thorough in your approach
9. When encountering errors, think carefully about the root cause before retrying

"""
        
        return prompt
    
    def _parse_llm_response(self, response: str, was_thinking_model: bool) -> List[ReActStep]:
        """Parse LLM response into ReAct steps."""
        steps = []
        model_used = self.config.get_litellm_model_name("thinking" if was_thinking_model else "action")
        
        # Split response into sections
        sections = re.split(r'\n\n(?=THOUGHT:|ACTION:|OBSERVATION:)', response.strip())
        
        for section in sections:
            section = section.strip()
            if not section:
                continue
            
            # Parse THOUGHT
            if section.startswith("THOUGHT:"):
                thought_content = section[8:].strip()
                steps.append(ReActStep(
                    step_type=StepType.THOUGHT,
                    content=thought_content,
                    timestamp=self._get_timestamp(),
                    model_used=model_used
                ))
            
            # Parse ACTION
            elif section.startswith("ACTION:"):
                action_lines = section.split('\n')
                if len(action_lines) < 2:
                    continue
                
                tool_name = action_lines[0][7:].strip()
                
                # Look for PARAMETERS line
                params = {}
                for line in action_lines[1:]:
                    if line.startswith("PARAMETERS:"):
                        try:
                            params_str = line[11:].strip()
                            if params_str:
                                params = json.loads(params_str)
                        except json.JSONDecodeError:
                            logger.warning(f"Failed to parse parameters: {params_str}")
                        break
                
                steps.append(ReActStep(
                    step_type=StepType.ACTION,
                    content=f"Using tool: {tool_name}",
                    tool_name=tool_name,
                    tool_params=params,
                    timestamp=self._get_timestamp(),
                    model_used=model_used
                ))
        
        return steps
    
    def _execute_steps(self, steps: List[ReActStep]) -> bool:
        """Execute a list of ReAct steps."""
        for step in steps:
            self.steps.append(step)
            
            if step.step_type == StepType.THOUGHT:
                self.agent_logger.info(f"💭 THOUGHT ({step.model_used}): {step.content[:100]}...")
                logger.info(f"💭 THOUGHT: {step.content[:100]}...")
                
                # Log to branch context if we're in one
                if isinstance(self.context_manager.current_context, BranchContext):
                    self.context_manager.current_context.add_log_entry(f"Thought: {step.content[:200]}...")
            
            elif step.step_type == StepType.ACTION:
                self.agent_logger.info(f"🔧 ACTION: {step.content}")
                logger.info(f"🔧 ACTION: {step.content}")
                
                if step.tool_name not in self.tools:
                    error_msg = f"Unknown tool: {step.tool_name}"
                    logger.error(error_msg)
                    self._add_observation_step(error_msg)
                    continue
                
                # Execute the tool
                tool = self.tools[step.tool_name]
                result = tool.safe_execute(**(step.tool_params or {}))
                
                step.tool_result = result
                
                # Add observation step
                self._add_observation_step(str(result))
                
                # Log to branch context if we're in one
                if isinstance(self.context_manager.current_context, BranchContext):
                    self.context_manager.current_context.add_log_entry(
                        f"Action: {step.tool_name} - {'Success' if result.success else 'Failed'}"
                    )
        
        return True
    
    def _add_observation_step(self, observation: str):
        """Add an observation step."""
        obs_step = ReActStep(
            step_type=StepType.OBSERVATION,
            content=observation,
            timestamp=self._get_timestamp()
        )
        self.steps.append(obs_step)
        
        # Log observation with truncation for console
        truncated_obs = observation[:200] + "..." if len(observation) > 200 else observation
        self.agent_logger.info(f"👁️ OBSERVATION: {truncated_obs}")
        logger.info(f"👁️ OBSERVATION: {truncated_obs}")
    
    def _is_task_complete(self) -> bool:
        """Check if the current task is complete."""
        # Look at recent steps for completion indicators
        recent_steps = self.steps[-5:] if len(self.steps) >= 5 else self.steps
        
        for step in recent_steps:
            if step.step_type == StepType.THOUGHT:
                content_lower = step.content.lower()
                if any(phrase in content_lower for phrase in [
                    "task completed", "setup complete", "finished", "done",
                    "successfully completed", "all tasks completed"
                ]):
                    return True
            
            elif step.step_type == StepType.ACTION and step.tool_name == "manage_context":
                # If we're switching to trunk with a completion summary
                if (step.tool_params and 
                    step.tool_params.get("action") == "switch_to_trunk" and
                    step.tool_params.get("summary")):
                    summary = step.tool_params.get("summary", "").lower()
                    if "completed" in summary or "success" in summary:
                        return True
        
        return False
    
    def _check_context_switching_guidance(self):
        """Check if we should provide context switching guidance."""
        if self.steps_since_context_switch >= self.context_switch_threshold:
            # Check if we're in a branch context and haven't switched recently
            if isinstance(self.context_manager.current_context, BranchContext):
                guidance = (
                    f"SYSTEM GUIDANCE: You have been working on the current task for "
                    f"{self.steps_since_context_switch} steps. Consider if the sub-task "
                    f"is complete and if you should return to the trunk context with a summary "
                    f"using the manage_context tool."
                )
                
                guidance_step = ReActStep(
                    step_type=StepType.SYSTEM_GUIDANCE,
                    content=guidance,
                    timestamp=self._get_timestamp()
                )
                self.steps.append(guidance_step)
                
                self.agent_logger.info(f"🔔 SYSTEM GUIDANCE: {guidance}")
                logger.info(f"🔔 SYSTEM GUIDANCE: Context switch suggestion")
                
                # Reset counter
                self.steps_since_context_switch = 0
    
    def _build_next_prompt(self) -> str:
        """Build the prompt for the next iteration."""
        prompt = "CONVERSATION HISTORY:\n\n"
        
        # Add recent steps (last 10 to keep context manageable)
        recent_steps = self.steps[-10:] if len(self.steps) > 10 else self.steps
        
        for step in recent_steps:
            if step.step_type == StepType.THOUGHT:
                prompt += f"THOUGHT: {step.content}\n\n"
            elif step.step_type == StepType.ACTION:
                prompt += f"ACTION: {step.tool_name}\n"
                if step.tool_params:
                    prompt += f"PARAMETERS: {json.dumps(step.tool_params)}\n\n"
            elif step.step_type == StepType.OBSERVATION:
                prompt += f"OBSERVATION: {step.content}\n\n"
            elif step.step_type == StepType.SYSTEM_GUIDANCE:
                prompt += f"SYSTEM GUIDANCE: {step.content}\n\n"
        
        prompt += "Continue with your next THOUGHT and ACTION:\n\n"
        return prompt
    
    def _get_timestamp(self) -> str:
        """Get current timestamp string."""
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    def get_execution_summary(self) -> Dict[str, Any]:
        """Get a summary of the execution."""
        thinking_actions = len([s for s in self.steps if s.model_used and "o1" in s.model_used])
        action_actions = len([s for s in self.steps if s.model_used and "o1" not in (s.model_used or "")])
        
        return {
            "total_steps": len(self.steps),
            "iterations": self.current_iteration,
            "thoughts": len([s for s in self.steps if s.step_type == StepType.THOUGHT]),
            "actions": len([s for s in self.steps if s.step_type == StepType.ACTION]),
            "observations": len([s for s in self.steps if s.step_type == StepType.OBSERVATION]),
            "thinking_model_calls": thinking_actions,
            "action_model_calls": action_actions,
            "successful_actions": len([
                s for s in self.steps 
                if s.step_type == StepType.ACTION and s.tool_result and s.tool_result.success
            ]),
            "failed_actions": len([
                s for s in self.steps 
                if s.step_type == StepType.ACTION and s.tool_result and not s.tool_result.success
            ])
        }

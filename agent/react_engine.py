"""ReAct Engine for Setup-Agent (SAG)."""

import json
import re
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from typing_extensions import deprecated

import litellm
from loguru import logger
from pydantic import BaseModel

from config import create_agent_logger, create_verbose_logger, get_config

from tools.base import BaseTool, ToolResult
from .context_manager import BranchContext, ContextManager, TrunkContext, BranchContextHistory
from .agent_state_evaluator import AgentStateEvaluator, AgentStateAnalysis, AgentStatus
from .output_storage import OutputStorageManager
from .physical_validator import PhysicalValidator


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

    def __init__(self, context_manager: ContextManager, tools: List[BaseTool], repository_url: str = None):
        self.context_manager = context_manager
        self.tools = {tool.name: tool for tool in tools}
        self.config = get_config()
        self.repository_url = repository_url

        # ReAct state
        self.steps: List[ReActStep] = []
        self.current_iteration = 0
        self.max_iterations = self.config.max_iterations

        # Context switching guidance
        self.steps_since_context_switch = 0
        self.context_switch_threshold = self.config.context_switch_threshold

        # Tool execution tracking to avoid repetitive calls
        self.recent_tool_executions = []
        self.max_recent_executions = 10
        self._force_thinking_next = False
        
        # CRITICAL: Flag to force thinking after successful tool execution
        self._force_thinking_after_success = False
        
        # State memory for successful operations
        self.successful_states = {
            'working_directory': None,  # Last successful working directory
            'cloned_repos': set(),      # Set of successfully cloned repo URLs
            'project_type': None,       # Detected project type
            'maven_success': False,     # Whether maven operations succeeded
        }

        # Agent logger for detailed traces
        self.agent_logger = create_agent_logger("react_engine")

        # Configure LiteLLM
        self._setup_litellm()

        # Check function calling support
        self._check_function_calling_support()
        
        # PERFORMANCE: Cache trunk context to avoid frequent file I/O
        self._cached_trunk_context = None
        self._trunk_context_cache_timestamp = None
        
        # Initialize the centralized state evaluator (will be updated with physical validator after initialization)
        self.state_evaluator = AgentStateEvaluator(self.context_manager)
        
        # Initialize output storage manager
        from pathlib import Path
        contexts_dir = Path(self.context_manager.contexts_dir) if hasattr(self.context_manager, 'contexts_dir') else Path("/workspace/.setup_agent/contexts")
        # Pass orchestrator to OutputStorageManager for container file operations
        orchestrator = self.context_manager.orchestrator if hasattr(self.context_manager, 'orchestrator') else None
        self.output_storage = OutputStorageManager(contexts_dir, orchestrator=orchestrator)
        
        # Initialize physical validator for fact-based validation
        self.physical_validator = PhysicalValidator(
            docker_orchestrator=orchestrator,
            project_path="/workspace"
        )
        
        # Update state evaluator with physical validator
        self.state_evaluator.physical_validator = self.physical_validator

        logger.info("ReAct Engine initialized with dual model support and physical validation")
        logger.info(f"Thinking model: {self.config.get_litellm_model_name('thinking')}")
        logger.info(f"Action model: {self.config.get_litellm_model_name('action')}")
        if repository_url:
            logger.info(f"Repository URL: {repository_url}")

    def set_repository_url(self, repository_url: str):
        """Set the repository URL for the current project."""
        self.repository_url = repository_url
        logger.info(f"Repository URL set: {repository_url}")

    def _setup_litellm(self):
        """Setup LiteLLM configuration."""
        # Set LiteLLM to not cache responses for debugging
        litellm.cache = None

        # Enable detailed logging for debugging
        if self.config.log_level.value == "DEBUG":
            litellm.set_verbose = True

        logger.info("LiteLLM configured")

    def _check_function_calling_support(self):
        """Check if the configured models support function calling."""
        action_model = self.config.get_litellm_model_name("action")
        thinking_model = self.config.get_litellm_model_name("thinking")

        # Check action model function calling support
        self.supports_function_calling = litellm.supports_function_calling(action_model)

        # Determine if this is a Claude model
        self.is_claude_model = (
            "claude" in action_model.lower() or "anthropic" in action_model.lower()
        )

        if self.supports_function_calling:
            model_type = "Claude" if self.is_claude_model else "OpenAI"
            logger.info(f"Action model {action_model} supports {model_type} function calling")
        else:
            logger.warning(
                f"Action model {action_model} does not support function calling, falling back to prompt-based approach"
            )
            # Enable fallback for models without function calling support
            litellm.add_function_to_prompt = True

        # Check parallel function calling support
        self.supports_parallel_function_calling = litellm.supports_parallel_function_calling(
            action_model
        )
        if self.supports_parallel_function_calling:
            logger.info(f"Action model {action_model} supports parallel function calling")
        else:
            logger.info(f"Action model {action_model} does not support parallel function calling")

    def _build_tools_schema(self) -> List[Dict[str, Any]]:
        """Build function calling schema from tools (supports both OpenAI and Claude formats)."""
        tools_schema = []

        for tool in self.tools.values():
            # Use the enhanced tool's parameter schema if available
            if hasattr(tool, "get_parameter_schema"):
                schema = tool.get_parameter_schema()
            else:
                # Fallback to basic schema for regular tools
                schema = {"type": "object", "properties": {}, "required": []}

            if self.is_claude_model:
                # Claude format - direct tool definition
                tool_def = {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": schema,
                }
            else:
                # OpenAI format - nested function definition
                tool_def = {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": schema,
                    },
                }

            tools_schema.append(tool_def)

        return tools_schema

    def _handle_function_calling_response(self, response, model: str) -> str:
        """Handle function calling response and convert to ReAct format (supports both OpenAI and Claude formats)."""
        try:
            message = response.choices[0].message

            # Extract thinking content if available
            content_parts = []
            if message.content:
                content_parts.append(f"THOUGHT: {message.content}")

            # Handle both OpenAI and Claude function calling formats
            if self.is_claude_model:
                # Claude format - tool_calls may be in different structure
                if hasattr(message, "tool_calls") and message.tool_calls:
                    for tool_call in message.tool_calls:
                        function_name = tool_call.get("name") or tool_call.get("function", {}).get(
                            "name"
                        )
                        function_args = tool_call.get("input") or tool_call.get("function", {}).get(
                            "arguments"
                        )

                        # Parse function arguments if they're a string
                        if isinstance(function_args, str):
                            try:
                                function_args = json.loads(function_args)
                            except json.JSONDecodeError:
                                logger.warning(
                                    f"Failed to parse Claude function arguments: {function_args}"
                                )
                                function_args = {}

                        if function_name:
                            # Format as ReAct ACTION
                            content_parts.append(f"ACTION: {function_name}")
                            content_parts.append(f"PARAMETERS: {json.dumps(function_args)}")
                            logger.debug(
                                f"Claude function call: {function_name} with args: {function_args}"
                            )

            else:
                # OpenAI format - standard tool_calls structure
                if hasattr(message, "tool_calls") and message.tool_calls:
                    for tool_call in message.tool_calls:
                        function_name = tool_call.function.name
                        
                        # Strip OpenAI namespace prefix if present
                        if function_name.startswith("functions."):
                            function_name = function_name[10:]  # Remove "functions." prefix

                        # Parse function arguments
                        try:
                            function_args = json.loads(tool_call.function.arguments)
                        except json.JSONDecodeError:
                            logger.warning(
                                f"Failed to parse OpenAI function arguments: {tool_call.function.arguments}"
                            )
                            function_args = {}

                        # Format as ReAct ACTION
                        content_parts.append(f"ACTION: {function_name}")
                        content_parts.append(f"PARAMETERS: {json.dumps(function_args)}")
                        logger.debug(
                            f"OpenAI function call: {function_name} with args: {function_args}"
                        )

            result = "\n\n".join(content_parts)

            # Log function calling usage
            if self.config.verbose:
                tool_count = (
                    len(message.tool_calls)
                    if hasattr(message, "tool_calls") and message.tool_calls
                    else 0
                )
                model_type = "Claude" if self.is_claude_model else "OpenAI"
                logger.info(
                    f"{model_type} function calling response from {model}: {tool_count} tool calls"
                )

            return result

        except Exception as e:
            logger.error(f"Failed to handle function calling response: {e}")
            # Fallback to regular content
            content = response.choices[0].message.content
            return content if content is not None else ""

    def run_react_loop(self, initial_prompt: str, max_iterations: Optional[int] = None) -> bool:
        """Run the main ReAct loop."""
        max_iter = max_iterations or self.max_iterations

        self.agent_logger.info(f"Starting ReAct loop with max {max_iter} iterations")

        # Initialize with the initial prompt
        self.steps = []
        self.current_iteration = 0
        
        # PERFORMANCE: Initialize trunk context cache at start
        self._invalidate_trunk_cache()  # Ensure fresh start
        self._get_cached_trunk_context()  # Load initial cache

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
                    logger.warning(f"Raw response was: {repr(response)}")
                    continue

                # Execute the steps
                success = self._execute_steps(parsed_steps)
                
                # CENTRALIZED STATE EVALUATION: Replace all scattered checks
                state_analysis = self.state_evaluator.evaluate(
                    steps=self.steps,
                    current_iteration=self.current_iteration,
                    recent_tool_executions=self.recent_tool_executions,
                    steps_since_context_switch=self.steps_since_context_switch
                )
                
                # Handle guidance based on state analysis
                if state_analysis.needs_guidance:
                    self._add_system_guidance(state_analysis.guidance_message, state_analysis.guidance_priority)
                
                # Check for task completion
                if state_analysis.is_task_complete:
                    self.agent_logger.info("Task completed successfully")
                    return True

                # DEPRECATED: Legacy checks now handled by state_evaluator
                # Check for context switching guidance
                # self._check_context_switching_guidance()
                
                # Check if model needs explicit action guidance
                # if self._needs_action_guidance():
                #     self._add_action_guidance()

                # Build prompt for next iteration
                current_prompt = self._build_next_prompt()

                # Step count is now automatically managed by branch history updates
                # No manual step increment needed in new design

                self.steps_since_context_switch += 1

            logger.warning(f"ReAct loop completed without success after {max_iter} iterations")
            return False

        except Exception as e:
            logger.error(f"ReAct loop failed: {e}", exc_info=True)
            return False

    def _should_use_thinking_model(self) -> bool:
        """Determine if we should use the thinking model for this step - ENFORCE REACT ARCHITECTURE."""
        # CRITICAL: Check if thinking model was requested after successful tool execution
        if self._force_thinking_after_success:
            self._force_thinking_after_success = False  # Reset the flag
            logger.info("Using thinking model to analyze successful tool execution results")
            return True
            
        # Check if thinking model was explicitly requested due to repetitive execution
        if self._force_thinking_next:
            self._force_thinking_next = False  # Reset the flag
            logger.info("Using thinking model due to repetitive execution detection")
            return True
        
        # CRITICAL: ReAct Architecture Enforcement
        # Thinking model = ANALYSIS and PLANNING (after observations)
        # Action model = EXECUTION (after thinking)
        
        # Always start with thinking model for initial analysis
        if len(self.steps) == 0:
            logger.info("Using thinking model for initial analysis")
            return True

        # ENFORCE PROPER REACT SEQUENCE: OBSERVATION → THINKING → ACTION → OBSERVATION
        last_step = self.steps[-1] if self.steps else None
        
        if last_step and last_step.step_type == StepType.OBSERVATION:
            # After observation, always analyze with thinking model
            logger.info("Using thinking model to analyze observation results")
            return True
            
        if last_step and last_step.step_type == StepType.THOUGHT:
            # After thinking, switch to action model for execution
            logger.info("Switching to action model for tool execution after analysis")
            return False

        # Use thinking model when we encounter errors (need analysis)
        recent_steps = self.steps[-3:] if len(self.steps) >= 3 else self.steps
        recent_errors = [
            s for s in recent_steps
            if s.step_type == StepType.ACTION and s.tool_result and not s.tool_result.success
        ]

        if len(recent_errors) >= 2:  # Lower threshold for quicker analysis
            logger.info("Using thinking model due to recent errors requiring analysis")
            return True

        # Default to action model for execution
        return False

    def _get_llm_response(self, prompt: str, use_thinking_model: bool = False) -> Optional[str]:
        """Get response from the appropriate LLM model."""
        try:
            if use_thinking_model:
                model = self.config.get_litellm_model_name("thinking")
                temperature = self.config.thinking_temperature
                max_tokens = self.config.thinking_max_tokens

                # Special handling for O-series models (only support temperature=1)
                if (
                    "o4" in self.config.thinking_model.lower()
                    or "o1" in self.config.thinking_model.lower()
                ):
                    temperature = 1.0

                # CRITICAL: Create specialized prompt for thinking model
                thinking_prompt = self._build_thinking_model_prompt(prompt)

                # Log detailed request in verbose mode
                if self.config.verbose:
                    self._log_llm_request(
                        model, thinking_prompt, temperature, max_tokens, use_thinking_model
                    )

                # Get thinking configuration based on provider
                thinking_config = self.config.get_thinking_config()

                # Special handling for different thinking models
                if thinking_config:
                    # For models with thinking capabilities (o1, claude)
                    response = litellm.completion(
                        model=model,
                        messages=[{"role": "user", "content": thinking_prompt}],
                        temperature=temperature,
                        max_tokens=max_tokens,
                        **thinking_config,
                    )
                else:
                    # For regular models
                    response = litellm.completion(
                        model=model,
                        messages=[{"role": "user", "content": thinking_prompt}],
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
            else:
                model = self.config.get_litellm_model_name("action")
                temperature = self.config.action_temperature
                max_tokens = self.config.action_max_tokens

                # Special handling for O-series models (only support temperature=1)
                if (
                    "o4" in self.config.action_model.lower()
                    or "o1" in self.config.action_model.lower()
                ):
                    temperature = 1.0

                # CRITICAL: Create specialized prompt for action model
                action_prompt = self._build_action_model_prompt(prompt)

                # Log detailed request in verbose mode
                if self.config.verbose:
                    self._log_llm_request(
                        model, action_prompt, temperature, max_tokens, use_thinking_model
                    )

                # Build parameters for the request
                request_params = {
                    "model": model,
                    "messages": [{"role": "user", "content": action_prompt}],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }

                # Add function calling support if available
                # For o4-mini and other supported models, use function calling for all steps
                if self.supports_function_calling:
                    tools_schema = self._build_tools_schema()
                    if tools_schema:
                        request_params["tools"] = tools_schema

                        # Different tool_choice format for Claude vs OpenAI
                        if self.is_claude_model:
                            request_params["tool_choice"] = {"type": "auto"}
                        else:
                            request_params["tool_choice"] = "auto"

                        model_type = "Claude" if self.is_claude_model else "OpenAI"
                        logger.debug(
                            f"Using {model_type} function calling with {len(tools_schema)} tools"
                        )
                        
                        # Log first tool schema for debugging
                        if tools_schema and self.config.verbose:
                            logger.debug(f"First tool schema: {json.dumps(tools_schema[0], indent=2)}")

                response = litellm.completion(**request_params)

            # Debug logging for function calling response
            message = response.choices[0].message
            logger.debug(f"Response message attributes: {dir(message)}")
            logger.debug(f"Has tool_calls: {hasattr(message, 'tool_calls')}")
            if hasattr(message, 'tool_calls'):
                logger.debug(f"Tool calls value: {message.tool_calls}")
            
            # Handle function calling response
            if (
                hasattr(response.choices[0].message, "tool_calls")
                and response.choices[0].message.tool_calls
            ):
                logger.debug("Using function calling response handler")
                return self._handle_function_calling_response(response, model)

            content = response.choices[0].message.content

            # Log detailed response in verbose mode
            if self.config.verbose:
                self._log_llm_response(model, content, response)

            content_str = content if content is not None else ""
            self.agent_logger.info(f"LLM Response from {model}: {len(content_str)} chars")
            
            # Always log the full response content
            logger.info(f"Full LLM Response from {model}:")
            logger.info(content_str)
            logger.debug(f"Model used: {model}, Response length: {len(content_str)}")

            # Fallback: Try to parse JSON function calls in content if function calling was expected
            if self.supports_function_calling and content_str.strip():
                parsed_content = self._try_parse_json_function_calls(content_str)
                if parsed_content:
                    logger.debug("Successfully parsed JSON function calls from content")
                    return parsed_content

            return content_str

        except Exception as e:
            logger.error(f"LLM request failed: {e}")
            if self.config.verbose:
                self._log_llm_error(e)
            return None

    def _try_parse_json_function_calls(self, content: str) -> Optional[str]:
        """Try to parse JSON function calls from content when function calling format is not used."""
        try:
            # Look for JSON patterns that might be function calls
            lines = content.strip().split('\n')
            parsed_parts = []
            i = 0
            
            while i < len(lines):
                stripped = lines[i].strip()
                
                # Check if this line contains a JSON object with various formats
                if stripped.startswith('{') and stripped.endswith('}'):
                    try:
                        json_obj = json.loads(stripped)
                        function_name = None
                        function_args = {}
                        
                        # Format 1: {"tool": "tool_name", "action": "action_name", ...}
                        if 'tool' in json_obj:
                            function_name = json_obj['tool']
                            # Convert all other fields to arguments, with special handling for common patterns
                            function_args = {k: v for k, v in json_obj.items() if k != 'tool'}
                            
                            # If there's only an action, it might be a parameter
                            if len(function_args) == 1 and 'action' in function_args:
                                function_args = {'action': function_args['action']}
                        
                        # Format 2: Standard format: {"name": "tool_name", "arguments": {...}}
                        elif 'name' in json_obj and 'arguments' in json_obj:
                            function_name = json_obj['name']
                            function_args = json_obj['arguments']
                        
                        # Format 2.5: Model thinking format: {"thought": "...", "action": "tool_name", "action_args": {...}}
                        elif 'action' in json_obj and 'action_args' in json_obj:
                            function_name = json_obj['action']
                            function_args = json_obj['action_args']
                            # Also include the thought as a separate line if present
                            if 'thought' in json_obj:
                                parsed_parts.append(f"THOUGHT: {json_obj['thought']}")
                        
                        # Format 2.6: Alternative args format: {"action": "tool_name", "args": {...}}
                        elif 'action' in json_obj and 'args' in json_obj:
                            function_name = json_obj['action']
                            function_args = json_obj['args']
                        
                        # Format 3: Single tool name format: {"manage_context": {...}}
                        elif len(json_obj) == 1:
                            tool_name = list(json_obj.keys())[0]
                            if tool_name in self.tools:
                                function_name = tool_name
                                function_args = json_obj[tool_name] if isinstance(json_obj[tool_name], dict) else {}
                        
                        # Format 4: Simple parameter object (assume it's for the last mentioned tool)
                        elif not function_name and any(key in json_obj for key in ['action', 'command', 'path', 'query']):
                            # Try to infer tool based on parameters
                            if 'action' in json_obj:
                                if json_obj.get('action') in ['read', 'write', 'append']:
                                    function_name = 'file_io'
                                elif json_obj.get('action') in ['get_info', 'switch_to_trunk', 'create_branch']:
                                    function_name = 'manage_context'
                                elif json_obj.get('action') in ['clone', 'detect_project_type']:
                                    function_name = 'project_setup'
                                function_args = json_obj
                            elif 'command' in json_obj:
                                # Check if it's a maven command or bash command
                                command = json_obj.get('command', '').lower()
                                if any(cmd in command for cmd in ['compile', 'test', 'package', 'clean', 'install']):
                                    function_name = 'maven'
                                else:
                                    function_name = 'bash'
                                function_args = json_obj
                            elif 'query' in json_obj:
                                function_name = 'web_search'
                                function_args = json_obj
                        
                        if function_name:
                            # Format as ReAct ACTION
                            parsed_parts.append(f"ACTION: {function_name}")
                            parsed_parts.append(f"PARAMETERS: {json.dumps(function_args)}")
                            logger.debug(f"Parsed JSON function call: {function_name} with args: {function_args}")
                            i += 1
                            continue
                            
                    except json.JSONDecodeError:
                        # Not valid JSON, treat as regular content
                        pass
                
                # Check for "ACTION:" followed by JSON on the next line
                if stripped == 'ACTION:' and i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if next_line.startswith('{') and next_line.endswith('}'):
                        try:
                            json_obj = json.loads(next_line)
                            function_name = None
                            function_args = {}
                            
                            # Try different JSON formats (same logic as above)
                            if 'name' in json_obj and 'arguments' in json_obj:
                                function_name = json_obj['name']
                                function_args = json_obj['arguments']
                            elif 'tool' in json_obj:
                                function_name = json_obj['tool']
                                function_args = {k: v for k, v in json_obj.items() if k != 'tool'}
                            
                            if function_name:
                                parsed_parts.append(f"ACTION: {function_name}")
                                parsed_parts.append(f"PARAMETERS: {json.dumps(function_args)}")
                                logger.debug(f"Parsed ACTION JSON: {function_name} with args: {function_args}")
                                i += 2  # Skip both lines
                                continue
                        except json.JSONDecodeError:
                            pass
                
                # Check for patterns like "ACTION: {json}" 
                if stripped.startswith('ACTION:'):
                    action_part = stripped[7:].strip()
                    if action_part.startswith('{') and action_part.endswith('}'):
                        try:
                            json_obj = json.loads(action_part)
                            function_name = None
                            function_args = {}
                            
                            # Try different JSON formats
                            if 'name' in json_obj and 'arguments' in json_obj:
                                function_name = json_obj['name']
                                function_args = json_obj['arguments']
                            elif 'tool' in json_obj:
                                function_name = json_obj['tool']
                                function_args = {k: v for k, v in json_obj.items() if k != 'tool'}
                            
                            if function_name:
                                parsed_parts.append(f"ACTION: {function_name}")
                                parsed_parts.append(f"PARAMETERS: {json.dumps(function_args)}")
                                logger.debug(f"Parsed ACTION JSON: {function_name} with args: {function_args}")
                                i += 1
                                continue
                        except json.JSONDecodeError:
                            pass
                
                # Check for markdown code blocks with JSON
                if stripped.startswith('```json') and i + 1 < len(lines):
                    # Find the end of the code block
                    json_lines = []
                    j = i + 1
                    while j < len(lines) and not lines[j].strip().startswith('```'):
                        json_lines.append(lines[j])
                        j += 1
                    
                    if json_lines:
                        try:
                            json_content = '\n'.join(json_lines).strip()
                            json_obj = json.loads(json_content)
                            function_name = None
                            function_args = {}
                            
                            # Try different JSON formats
                            if 'name' in json_obj and 'arguments' in json_obj:
                                function_name = json_obj['name']
                                function_args = json_obj['arguments']
                            elif 'tool' in json_obj:
                                function_name = json_obj['tool']
                                function_args = {k: v for k, v in json_obj.items() if k != 'tool'}
                            
                            if function_name:
                                parsed_parts.append(f"ACTION: {function_name}")
                                parsed_parts.append(f"PARAMETERS: {json.dumps(function_args)}")
                                logger.debug(f"Parsed JSON code block: {function_name} with args: {function_args}")
                                i = j + 1  # Skip past the closing ```
                                continue
                        except json.JSONDecodeError:
                            pass
                
                # Keep non-JSON lines as-is (thoughts, etc.)
                if stripped and not stripped.startswith('```'):
                    parsed_parts.append(stripped)
                
                i += 1
            
            if parsed_parts:
                return '\n'.join(parsed_parts)
                
        except Exception as e:
            logger.warning(f"Failed to parse JSON function calls: {e}")
            
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

"""

        # Add repository URL at the very beginning if available
        if self.repository_url:
            prompt += f"""🚨 IMPORTANT PROJECT INFORMATION 🚨
Repository URL: {self.repository_url}
This URL is ALREADY PROVIDED - DO NOT ASK FOR IT AGAIN!
Your first action should be to clone this repository using the project_setup tool.

"""

        prompt += """CRITICAL CONTEXT MANAGEMENT RULES:
- You work with TWO types of contexts: TRUNK (main) and BRANCH (task-specific)
- TRUNK context: Contains the overall goal and TODO list of tasks
- BRANCH context: For focused work on ONE specific task from the TODO list
- COMPLETE WORKFLOW CYCLE:
  1. manage_context(action="get_info") → Check current state and TODO list
  2. manage_context(action="start_task", task_id="...") → Start next pending task
  3. [Do the actual work for the task]
  4. manage_context(action="complete_with_results", summary="...", key_results="...") → Complete task
  5. REPEAT FROM STEP 1 → Check for next task
- CRITICAL: After completing ANY task, IMMEDIATELY call manage_context(action="get_info") to see next tasks!
- The system will show you the next pending task after completion - FOLLOW THAT GUIDANCE!
- This prevents "ghost states" where work is done but not officially recorded
- NO need for 'branch_start', 'branch_end', 'create_branch' - the system handles context switching!
- Context switching is AUTOMATIC - you only specify which task to work on
- The key_results from completed tasks will guide your next actions

AVAILABLE TOOLS:
"""

        # Add tool descriptions with usage examples
        for tool in self.tools.values():
            prompt += f"\n- {tool.name}: {tool.description}"
            if hasattr(tool, "get_usage_example"):
                prompt += f"\n  Usage: {tool.get_usage_example()}"

        # Add explicit tool name clarification
        prompt += """

AVAILABLE TOOLS:
- bash: Execute shell commands (NOT shell, run_shell, git_clone, or python)
- file_io: Read, write, append, and list files in Docker container (NOT read_file or write_file)
- web_search: Search the web for information
- manage_context: Manage task workflow and context (NOT context or complete_with_results)
  • Valid actions: get_info, start_task, complete_task, complete_with_results, add_context, get_full_context
  • start_task: Begin work on a specific task ID from TODO list
  • complete_with_results: RECOMMENDED action - Finish current task with key results (ATOMIC operation)
  • complete_task: Basic task completion (use action="complete_with_results" instead)
  • get_info: Check current context and available tasks
  • CRITICAL: complete_with_results is an ACTION, not a separate tool!
  • Example: manage_context(action="complete_with_results", summary="Task completed successfully", key_results="Built project, all tests pass")
- maven: Execute Maven commands (NOT mvn)
- project_setup: Clone repositories and setup projects (NOT git_clone or clone)
- project_analyzer: 🆕 AUTOMATIC PROJECT ANALYSIS - MUST be called immediately after every successful clone
  • 🔥 TRIGGER: Automatically call this tool after ANY project_setup clone success
  • 🔥 MANDATORY: Do not skip this step - prevents build failures and generates optimal plans
  • Analyzes Maven (pom.xml), Gradle (build.gradle/build.gradle.kts), Node.js (package.json), Python configs
  • Detects Java versions, dependencies, test frameworks (JUnit, TestNG, Spock), reads README instructions
  • Intelligently replaces your generic TODO tasks with project-specific optimized task sequences
  • Example: project_setup success → project_analyzer(action="analyze", project_path="/workspace/project-name")
  • Result: Generic tasks → Smart tasks like "Setup Java 17", "Maven clean install", "Run JUnit tests"
- system: Install system packages and dependencies

🧠 INTELLIGENT SETUP WORKFLOW (MANDATORY AND AUTOMATIC):
1. 📥 Clone repository with project_setup tool
2. 🔍 IMMEDIATELY and AUTOMATICALLY use project_analyzer tool after ANY successful clone
   ⚠️  CRITICAL: This is NOT optional - project_analyzer MUST be called after every clone success
   ⚠️  Do NOT attempt any build/test commands before project analysis
   ⚠️  Do NOT skip this step - it prevents failed executions and generates optimal plans
3. 📋 The analyzer will automatically replace your generic tasks with intelligent, project-specific tasks
4. ✅ Execute the generated optimized task plan
5. 📊 Generate final report

AUTOMATIC TRIGGER RULE:
✅ project_setup(action="clone") → SUCCESS → 🔍 project_analyzer(action="analyze") [MANDATORY]

Why this automation is critical:
• Detects project type (Maven, Gradle, Node.js, Python, etc.) and Java versions
• Reads README files and build configurations for specific instructions  
• Generates optimized task sequences instead of generic build attempts
• Prevents the "wrong directory" and "unknown project type" failures
• Replaces your TODO list with intelligent, project-specific tasks

EXAMPLE FLOW:
1. project_setup(action="clone", repository_url="...", target_directory="my-project")
2. ✅ Clone successful → IMMEDIATELY call project_analyzer(action="analyze", project_path="/workspace/my-project") 
3. 📋 Your generic tasks get replaced with smart tasks like "Setup Maven Java 17 environment", "Build with Maven", etc.
4. Execute the intelligent plan

Use these tools as needed to complete your tasks."""

        prompt += f"""

CURRENT CONTEXT:
Context Type: {context_info.get('context_type', 'unknown')}
Context ID: {context_info.get('context_id', 'unknown')}
"""

        if context_info.get("context_type") == "trunk":
            prompt += f"""
Goal: {context_info.get('goal', 'Not specified')}
Progress: {context_info.get('progress', 'Not available')}
Next Task: {context_info.get('next_task', 'No pending tasks')}
"""
        elif context_info.get("context_type") == "branch":
            prompt += f"""
Current Task: {context_info.get('task', 'Not specified')}
Current Focus: {context_info.get('focus', 'Not specified')}
"""

        # Add different instructions based on function calling support
        if self.supports_function_calling:
            prompt += """

RESPONSE FORMAT:
You have access to function calling capabilities. Use the tools directly - they will be executed automatically.

IMPORTANT GUIDELINES:
1. USE THE TOOLS! Don't just think about using them - actually call them!
2. Use the available tools through function calling to execute actions
3. You can provide reasoning in your response content before or after tool calls
4. Use manage_context tool correctly:
   • NEVER use "switch" action - use "switch_to_trunk" instead
   • For create_branch: ALWAYS provide task_id parameter
   • For get_info: No additional parameters needed
5. In TRUNK context: analyze TODO list and create branch contexts for tasks
6. In BRANCH context: focus on the specific task, use detailed logging
7. Always provide summaries when returning to trunk context
8. Use bash tool for system operations, file_io for file operations
9. Use web_search when you encounter unknown errors or need documentation
10. When encountering errors, think carefully about the root cause before retrying

MANDATORY WORKFLOW FOR PROJECT SETUP:
🔄 CORRECT TASK MANAGEMENT FLOW:
1. ALWAYS start with: manage_context(action="get_info") - check current status
2. If you see TODO tasks, MUST start first task: manage_context(action="start_task", task_id="task_1")
3. IN TASK CONTEXT: Execute the actual work:
   • project_setup(action="clone", repository_url="...") 
   • 🔥 IMMEDIATELY after clone success: project_analyzer(action="analyze")
   • Follow the intelligent plan generated by analyzer
4. When task work is done: manage_context(action="complete_with_results", summary="Brief task summary", key_results="Specific achievements and findings")
5. System automatically moves to next task - repeat from step 2

⚠️ CRITICAL RULE: NEVER try to complete_task without first doing start_task!
⚠️ ALL project work must happen INSIDE a task context (after start_task)

REQUIRED SEQUENCE: 
1. manage_context(action="get_info") - See TODO list
2. manage_context(action="start_task", task_id="...") - Start task
3. [do actual work] - Execute task
4. manage_context(action="complete_with_results", summary="...", key_results="...") - Complete task
5. GO BACK TO SEQUENCE STEP 1 - Check for next task (DON'T SKIP THIS!)"""
        else:
            prompt += """

RESPONSE FORMAT:
Always respond in this exact format:

THOUGHT: [Your deep reasoning about what to do next, analyze the situation thoroughly]

ACTION: [tool_name]
PARAMETERS: [JSON object with parameters]

Wait for OBSERVATION, then continue with next THOUGHT/ACTION cycle.

IMPORTANT GUIDELINES:
1. Always start with THOUGHT to explain your reasoning
2. Use manage_context tool correctly:
   • NEVER use "switch" action - use "switch_to_trunk" instead
   • For create_branch: ALWAYS provide task_id parameter
   • For get_info: No additional parameters needed
3. In TRUNK context: analyze TODO list and create branch contexts for tasks
4. In BRANCH context: focus on the specific task, use detailed logging
5. Always provide summaries when returning to trunk context
6. Use bash tool for system operations, file_io for file operations
7. Use web_search when you encounter unknown errors or need documentation
8. Be methodical and thorough in your approach
9. When encountering errors, think carefully about the root cause before retrying

MANDATORY WORKFLOW FOR PROJECT SETUP:
🔄 CORRECT TASK MANAGEMENT FLOW:
1. ALWAYS start with: manage_context(action="get_info") - check current status  
2. If you see TODO tasks, MUST start first task: manage_context(action="start_task", task_id="task_1")
3. IN TASK CONTEXT: Execute the actual work (project_setup, project_analyzer, etc.)
4. When done: manage_context(action="complete_with_results", summary="...", key_results="...")
5. System automatically moves to next task - repeat from step 2

⚠️ CRITICAL: NEVER try to complete_task without first doing start_task!"""

        
        # Add repository URL reminder if available
        if self.repository_url:
            prompt += f"""

📂 REPOSITORY INFO: The target repository is {self.repository_url}
🔧 Use this URL when cloning: project_setup(action="clone", repository_url="{self.repository_url}")"""
        
        prompt += """

🔄 REMEMBER THE CONTINUOUS CYCLE: 
   manage_context(action="get_info") → manage_context(action="start_task") → [do work] → manage_context(action="complete_with_results") → BACK TO get_info → start next task → [repeat]
   
⚠️ AFTER COMPLETING EACH TASK: Always call manage_context(action="get_info") to see the next task!

"""

        return prompt

    def _parse_llm_response(self, response: str, was_thinking_model: bool) -> List[ReActStep]:
        """Parse LLM response into ReAct steps."""
        steps = []
        model_used = self.config.get_litellm_model_name(
            "thinking" if was_thinking_model else "action"
        )

        # Log the raw response for debugging
        logger.debug(f"Parsing LLM response: {repr(response)}")

        # Split response into sections
        sections = re.split(r"\n\n(?=THOUGHT:|ACTION:|OBSERVATION:)", response.strip())
        
        logger.debug(f"Split response into {len(sections)} sections")
        for i, section in enumerate(sections):
            logger.debug(f"Section {i+1}: {section}")

        for section in sections:
            section = section.strip()
            if not section:
                continue

            # Parse THOUGHT
            if section.startswith("THOUGHT:"):
                thought_content = section[8:].strip()
                steps.append(
                    ReActStep(
                        step_type=StepType.THOUGHT,
                        content=thought_content,
                        timestamp=self._get_timestamp(),
                        model_used=model_used,
                    )
                )

            # Parse ACTION
            elif section.startswith("ACTION:"):
                action_lines = section.split("\n")
                if len(action_lines) < 2:
                    continue

                tool_name = action_lines[0][7:].strip()
                
                # Check for invalid tool names
                if not tool_name or tool_name.lower() in ["none", "null", ""]:
                    # Convert to a thought with guidance
                    thought_content = "I need to take action but haven't specified a valid tool."
                    steps.append(
                        ReActStep(
                            step_type=StepType.THOUGHT,
                            content=thought_content,
                            timestamp=self._get_timestamp(),
                            model_used=model_used,
                        )
                    )
                    continue

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

                steps.append(
                    ReActStep(
                        step_type=StepType.ACTION,
                        content=f"Using tool: {tool_name}",
                        tool_name=tool_name,
                        tool_params=params,
                        timestamp=self._get_timestamp(),
                        model_used=model_used,
                    )
                )

        # If no steps were parsed, try to extract at least a thought
        if not steps and response.strip():
            content = response.strip()
            logger.info(f"Parsing failed, treating entire response as thought")
            logger.info(f"Full response content: {content}")
            
            # CRITICAL: Maintain ReAct structure - thinking should lead to action in next iteration
            # Add system guidance to ensure proper model role separation
            if was_thinking_model:
                # Thinking model should not attempt actions - guide towards pure analysis
                enhanced_content = content + "\n\n[SYSTEM: This was pure analysis. Next step should be action execution by action model.]"
            else:
                # Action model failed to format properly - provide formatting guidance
                enhanced_content = content + "\n\n[SYSTEM: Action model must use proper tool call format: ACTION: tool_name, PARAMETERS: {...}]"
            
            steps.append(
                ReActStep(
                    step_type=StepType.THOUGHT,
                    content=enhanced_content,
                    timestamp=self._get_timestamp(),
                    model_used=model_used,
                )
            )

        return steps

    def _execute_steps(self, steps: List[ReActStep]) -> bool:
        """Execute a list of ReAct steps."""
        for step in steps:
            self.steps.append(step)

            if step.step_type == StepType.THOUGHT:
                self.agent_logger.info(f"💭 THOUGHT ({step.model_used}): {step.content}")
                logger.info(f"💭 THOUGHT: {step.content}")

                # Detailed logging in verbose mode
                if self.config.verbose:
                    self._log_react_step_verbose(step)

                # Log to branch context if we're in one
                if self.context_manager.current_task_id:
                    # Add thought to branch history using new context management system
                    try:
                        self.context_manager.add_to_branch_history(
                            self.context_manager.current_task_id,
                            {"type": "thought", "content": step.content}
                        )
                    except Exception as e:
                        logger.warning(f"Failed to log thought to branch history: {e}")

            elif step.step_type == StepType.ACTION:
                self.agent_logger.info(f"🔧 ACTION: {step.content}")
                logger.info(f"🔧 ACTION: {step.content}")

                # Detailed logging in verbose mode
                if self.config.verbose:
                    self._log_react_step_verbose(step)

                if step.tool_name not in self.tools:
                    error_msg = f"Unknown tool: {step.tool_name}"
                    logger.error(error_msg)
                    self._add_observation_step(error_msg)
                    continue

                # Execute the tool with parameter validation and self-healing
                tool = self.tools[step.tool_name]
                
                # Validate and fix parameters
                validated_params = self._validate_and_fix_parameters(step.tool_name, step.tool_params or {})

                # Check for repetitive tool execution with ENHANCED graduated response
                tool_signature = f"{step.tool_name}:{str(sorted(validated_params.items()))}"
                repetition_level = self._get_repetition_level(tool_signature)
                
                if repetition_level > 0:
                    recent_executions = [e for e in self.recent_tool_executions 
                                       if e["signature"].startswith(step.tool_name + ':')]
                    failure_count = sum(1 for e in recent_executions if not e["success"])
                    
                    # Level 1: Warning (3 repetitions)
                    if repetition_level == 1:
                        logger.warning(f"Repetition level 1: {step.tool_name} called {len(recent_executions)} times")
                        # Continue execution but with warning
                        
                    # Level 2: Guidance (4 repetitions)
                    elif repetition_level == 2:
                        logger.warning(f"Repetition level 2: {step.tool_name} needs alternative approach")
                        self._force_thinking_next = True
                        guidance_msg = (f"Tool {step.tool_name} has been executed {len(recent_executions)} times with {failure_count} failures. "
                                      f"Consider alternative approaches: ")
                        guidance_msg += self._generate_alternative_suggestions(step.tool_name, validated_params, recent_executions)
                        
                    # Level 3: Force break (5+ repetitions)
                    else:  # repetition_level >= 3
                        logger.error(f"BREAKING INFINITE LOOP: {step.tool_name} called {len(recent_executions)} times")
                        
                        # Check for specific patterns and apply targeted fixes
                        if "update-alternatives" in str(recent_executions) or "java" in step.tool_name.lower():
                            logger.info("Detected Java configuration loop - attempting auto-fix")
                            return self._auto_fix_java_configuration()
                        
                        # Force progression to next task
                        logger.info("Forcing progression to next task to break loop")
                        result = ToolResult(
                            success=False,
                            output=f"🛑 INFINITE LOOP BROKEN: {step.tool_name} was called {len(recent_executions)} times without progress.\n"
                                  f"Failures: {failure_count}/{len(recent_executions)}\n"
                                  f"Moving to next task to prevent resource waste.",
                            error="Infinite loop detected and broken",
                            error_code="INFINITE_LOOP_BROKEN",
                            suggestions=[
                                "Task has been marked as incomplete",
                                "Proceeding with next task",
                                "Review logs for root cause analysis"
                            ]
                        )
                        
                        # Update tool execution history
                        self._update_tool_execution_history(tool_signature, False)
                        
                        # Force context manager to move to next task
                        if hasattr(self.context_manager, 'force_next_task'):
                            self.context_manager.force_next_task()
                        
                        return result
                    
                    guidance_msg = (f"Tool {step.tool_name} has been executed repeatedly with {failure_count} failures. "
                                  f"This suggests the current approach is not working. ")
                    
                    if step.tool_name == "maven":
                        guidance_msg += ("Consider checking the project structure, examining build errors in detail, "
                                       "or switching to bash tool to investigate the issue manually.")
                    elif step.tool_name == "bash":
                        guidance_msg += ("Consider checking command syntax, working directory, or using file_io "
                                       "to examine files before executing commands.")
                    elif step.tool_name == "manage_context":
                        action = validated_params.get("action", "")
                        if action == "complete_with_results":
                            # Check if parameters are actually missing
                            has_summary = "summary" in validated_params
                            has_key_results = "key_results" in validated_params
                            
                            if not has_summary or not has_key_results:
                                missing = []
                                if not has_summary:
                                    missing.append("summary")
                                if not has_key_results:
                                    missing.append("key_results")
                                guidance_msg += (f'Missing required parameters: {", ".join(missing)}. '
                                               'The action "complete_with_results" requires both summary and key_results. '
                                               'Example: manage_context(action="complete_with_results", '
                                               'summary="Analyzed project structure", key_results="Project type: Maven, Build system: pom.xml found")')
                            else:
                                guidance_msg += ('Parameters appear correct but execution is failing. '
                                               'Try simpler values or check if task is already completed.')
                        else:
                            guidance_msg += ("Check that all required parameters are provided for the action. "
                                           "Use manage_context(action=\"get_info\") to see current state.")
                    else:
                        guidance_msg += ("Consider examining the error messages, changing parameters, "
                                       "or using a different tool to achieve the same goal.")
                    
                    # Still execute the tool but with warning
                    # This ensures agent can see actual errors instead of empty output
                    try:
                        actual_result = tool.execute(**validated_params)
                        # Prepend warning to actual output
                        warning_prefix = f"⚠️ REPETITIVE EXECUTION WARNING: {step.tool_name} has been called {len(recent_executions)} times\n\n"
                        result = ToolResult(
                            success=actual_result.success,
                            output=warning_prefix + actual_result.output,
                            raw_output=actual_result.raw_output if hasattr(actual_result, 'raw_output') else actual_result.output,
                            error=actual_result.error if hasattr(actual_result, 'error') else None,
                            error_code="REPETITIVE_EXECUTION_WITH_OUTPUT",
                            metadata=actual_result.metadata if hasattr(actual_result, 'metadata') else {}
                        )
                    except Exception as e:
                        # If execution fails, at least show the error
                        result = ToolResult(
                            success=False,
                            output=f"⚠️ Repetitive execution detected. Tool execution failed: {str(e)}",
                            error=guidance_msg,
                            error_code="REPETITIVE_EXECUTION",
                            suggestions=[
                            "Use thinking model to analyze the root cause of repeated failures",
                            "Try a different approach or tool to achieve the same goal",
                            "Examine the full error output using raw_output=true if available",
                            "Use file_io or bash tools to investigate the environment manually"
                            ]
                        )
                else:
                    # Log tool execution in verbose mode
                    if self.config.verbose:
                        self._log_tool_execution_verbose(step.tool_name, validated_params)

                    result = tool.safe_execute(**validated_params)
                    
                    # Track this execution
                    self._track_tool_execution(tool_signature, result.success)
                    
                    # ENHANCED: Intelligent error recovery mechanism
                    if not result.success:
                        recovery_result = self._attempt_error_recovery(step.tool_name, validated_params, result)
                        if recovery_result["attempted"]:
                            logger.info(f"🔧 Error recovery attempted: {recovery_result['message']}")
                            if recovery_result["success"]:
                                # Recovery succeeded, use the recovered result
                                result = recovery_result["result"]
                                logger.info(f"✅ Error recovery successful for {step.tool_name}")
                    
                    # Update successful states for future reference
                    if result.success:
                        self._update_successful_states(step.tool_name, validated_params, result)
                        
                        # PERFORMANCE: Invalidate trunk cache if context state changed
                        if step.tool_name == "manage_context":
                            action = validated_params.get("action", "")
                            if action in ["start_task", "complete_task", "complete_with_results"]:
                                self._invalidate_trunk_cache()
                                logger.debug(f"Trunk cache invalidated after {action}")

                step.tool_result = result

                # Log tool result in verbose mode
                if self.config.verbose:
                    self._log_tool_result_verbose(step.tool_name, result)

                # Add observation step with improved formatting
                self._add_observation_step(self._format_tool_result(step.tool_name, result))
                
                # CRITICAL: Force thinking after successful tool execution to prevent cognitive rush
                if result.success:
                    self._force_thinking_after_success = True
                    logger.debug(f"✅ Tool {step.tool_name} succeeded - forcing thinking on next iteration")

                # Log to branch context if we're in one
                if self.context_manager.current_task_id:
                    # Add action result to branch history using new context management system
                    try:
                        output_to_store = result.output if result.output else ""
                        from datetime import datetime
                        timestamp = datetime.now().isoformat()
                        
                        # Store full output and get reference if output is large
                        if len(output_to_store) > 800:
                            # Store the full output
                            ref_id = self.output_storage.store_output(
                                task_id=self.context_manager.current_task_id,
                                tool_name=step.tool_name,
                                output=output_to_store,
                                timestamp=timestamp,
                                metadata={
                                    "success": result.success,
                                    "iteration": self.current_iteration
                                }
                            )
                            
                            # Get truncated version with reference
                            output_to_store = self.output_storage.get_truncation_with_reference(
                                output=output_to_store,
                                ref_id=ref_id,
                                max_length=800,
                                tool_name=step.tool_name
                            )
                        
                        self.context_manager.add_to_branch_history(
                            self.context_manager.current_task_id,
                            {
                                "type": "action",
                                "tool_name": step.tool_name,
                                "success": result.success,
                                "output": output_to_store
                            }
                        )
                    except Exception as e:
                        logger.warning(f"Failed to log action to branch history: {e}")

        return True

    def _validate_and_fix_parameters(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and fix tool parameters with self-healing capability."""
        if tool_name not in self.tools:
            logger.error(f"Unknown tool: {tool_name}")
            return params
            
        tool = self.tools[tool_name]
        
        # Handle completely empty parameters
        if not params:
            params = {}
        
        # Get the tool's parameter schema
        try:
            if hasattr(tool, 'get_parameter_schema'):
                schema = tool.get_parameter_schema()
            elif hasattr(tool, '_get_parameters_schema'):
                schema = tool._get_parameters_schema()
            else:
                # No schema available, apply basic fixes
                return self._apply_basic_parameter_fixes(tool_name, params)
                
            # Validate and fix parameters
            validated_params = self._fix_parameters_against_schema(params, schema, tool_name)
            
            # Apply additional tool-specific fixes
            validated_params = self._apply_tool_specific_fixes(tool_name, validated_params)
            
            # Check for unexpected parameters and provide warnings
            expected_params = set(schema.get('properties', {}).keys())
            actual_params = set(validated_params.keys())
            unexpected_params = actual_params - expected_params
            
            if unexpected_params:
                logger.warning(f"🚨 Unexpected parameters for {tool_name}: {unexpected_params}")
                logger.warning(f"Expected parameters: {expected_params}")
                
                # Only remove parameters that are clearly invalid, keep potentially useful ones
                params_to_remove = []
                for param in unexpected_params:
                    param_value = validated_params[param]
                    
                    # Keep parameters that might be useful extensions
                    if tool_name == "maven" and param in ["pom_file", "maven_home", "java_home"]:
                        logger.info(f"🔧 Keeping potentially useful Maven parameter: {param}={param_value}")
                        continue
                    elif tool_name == "bash" and param in ["env", "environment"]:
                        logger.info(f"🔧 Keeping potentially useful bash parameter: {param}={param_value}")
                        continue
                    elif tool_name == "system" and param in ["sudo", "force"]:
                        logger.info(f"🔧 Keeping potentially useful system parameter: {param}={param_value}")
                        continue
                    else:
                        # Remove clearly invalid parameters
                        params_to_remove.append(param)
                
                # DISABLED: Auto-removal of invalid parameters to enable proper error feedback
                # Let tools handle their own parameter validation and provide clear error messages
                # for param in params_to_remove:
                #     logger.warning(f"🔧 Removing invalid parameter: {param}={validated_params[param]}")
                #     del validated_params[param]
            
            # Log parameter fixes if any were made
            if validated_params != params:
                logger.info(f"🔧 Parameter self-healing applied for {tool_name}")
                logger.debug(f"Original params: {params}")
                logger.debug(f"Fixed params: {validated_params}")
                
            return validated_params
            
        except Exception as e:
            logger.warning(f"Failed to validate parameters for {tool_name}: {e}")
            return self._apply_basic_parameter_fixes(tool_name, params)

    def _fix_parameters_against_schema(self, params: Dict[str, Any], schema: Dict[str, Any], tool_name: str) -> Dict[str, Any]:
        """Fix parameters against a schema with intelligent defaults."""
        fixed_params = params.copy()
        
        # Get schema properties
        properties = schema.get('properties', {})
        required = schema.get('required', [])
        
        # Fix missing required parameters
        for param_name in required:
            if param_name not in fixed_params or fixed_params[param_name] is None:
                default_value = self._get_smart_default(param_name, properties.get(param_name, {}), tool_name)
                if default_value is not None:
                    fixed_params[param_name] = default_value
                    logger.info(f"🔧 Added missing required parameter '{param_name}' with default: {default_value}")
        
        # Fix parameter types
        for param_name, param_value in fixed_params.items():
            if param_name in properties:
                prop_schema = properties[param_name]
                expected_type = prop_schema.get('type')
                
                # Try to convert to expected type
                if expected_type and param_value is not None:
                    converted_value = self._convert_parameter_type(param_value, expected_type, param_name)
                    if converted_value != param_value:
                        fixed_params[param_name] = converted_value
                        logger.info(f"🔧 Converted parameter '{param_name}' from {type(param_value).__name__} to {expected_type}")
        
        # Handle common parameter naming issues
        fixed_params = self._fix_parameter_names(fixed_params, properties, tool_name)
        
        return fixed_params

    def _get_smart_default(self, param_name: str, param_schema: Dict[str, Any], tool_name: str) -> Any:
        """Get smart default values for common parameters."""
        param_type = param_schema.get('type', 'string')
        
        # Check if there's a default in the schema
        if 'default' in param_schema:
            return param_schema['default']
        
        # Smart defaults based on parameter names and tool types
        smart_defaults = {
            # Command-related parameters
            'command': 'help' if tool_name == 'bash' else None,
            'cmd': 'help',
            'timeout': 60,
            
            # File-related parameters
            'action': self._get_tool_specific_action_default(tool_name),
            'path': '/workspace',
            'file_path': '/workspace',
            'directory': '/workspace',
            'working_directory': '/workspace',
            
            # Web search parameters
            'query': 'help' if tool_name == 'web_search' else None,
            'max_results': 5,
            
            # System parameters
            'packages': [] if param_type == 'array' else None,
            
            # Maven parameters
            'goals': None,
            'profiles': None,
            'properties': None,
            'raw_output': False,
            
            # Context management
            'context_type': 'branch',
            'summary': 'Task in progress',
            
            # Project setup parameters - DO NOT provide defaults for URLs
            # These should come from the user's actual repository URL
            'repository_url': None,
            'url': None,
            'repo_url': None,
            
            # Generic defaults by type
            'boolean': False,
            'integer': 0,
            'array': [],
            'object': {}
        }
        
        # Try parameter name first
        if param_name in smart_defaults:
            return smart_defaults[param_name]
        
        # Try parameter type
        if param_type in smart_defaults:
            return smart_defaults[param_type]
        
        return None

    def _get_tool_specific_action_default(self, tool_name: str) -> str:
        """Get tool-specific default action."""
        tool_action_defaults = {
            'file_io': 'read',
            'project_setup': 'clone',
            'system': 'install_missing',
            'manage_context': 'get_info',
            'maven': 'compile',
            'bash': None,
            'web_search': None
        }
        return tool_action_defaults.get(tool_name, 'list')

    def _convert_parameter_type(self, value: Any, expected_type: str, param_name: str) -> Any:
        """Convert parameter to expected type."""
        try:
            if expected_type == 'string':
                return str(value)
            elif expected_type == 'integer':
                if isinstance(value, str):
                    # Try to extract number from string
                    import re
                    match = re.search(r'\d+', value)
                    if match:
                        return int(match.group())
                return int(value)
            elif expected_type == 'boolean':
                if isinstance(value, str):
                    return value.lower() in ['true', '1', 'yes', 'on']
                return bool(value)
            elif expected_type == 'array':
                if isinstance(value, str):
                    # Try to parse as JSON array or split by common delimiters
                    try:
                        import json
                        return json.loads(value)
                    except:
                        # Split by common delimiters
                        return [item.strip() for item in value.split(',')]
                elif not isinstance(value, list):
                    return [value]
                return value
            elif expected_type == 'object':
                if isinstance(value, str):
                    try:
                        import json
                        return json.loads(value)
                    except:
                        # CRITICAL FIX: Don't lose the original string value!
                        # For manage_context entry parameter, wrap string in meaningful object
                        if param_name == 'entry':
                            return {"content": value}  # Preserve the original string as content
                        elif 'description' in param_name.lower() or 'content' in param_name.lower():
                            return {"description": value}
                        else:
                            return {"value": value}  # Fallback: preserve in generic wrapper
                return value if isinstance(value, dict) else {"value": value}
        except Exception as e:
            logger.warning(f"Failed to convert parameter '{param_name}' to {expected_type}: {e}")
            return value
        
        return value

    def _fix_parameter_names(self, params: Dict[str, Any], properties: Dict[str, Any], tool_name: str) -> Dict[str, Any]:
        """Fix common parameter naming issues."""
        fixed_params = params.copy()
        
        # Common parameter name mappings (removed conflicting mappings)
        name_mappings = {
            # Action variations (file_io, context tools)
            'op': 'action',
            'operation': 'action',
            'method': 'action',
            'type': 'action',
            
            # Query variations (web_search tool)
            'search': 'query',
            'q': 'query',
            'term': 'query',
            'search_term': 'query',
            'keywords': 'query',
            
            # URL variations (project_setup tool)
            'url': 'repository_url',
            'repo_url': 'repository_url',
            'git_url': 'repository_url',
            'repository': 'repository_url',
            'repo': 'repository_url',
            'git_repo': 'repository_url',
            
            # Target directory variations (project_setup tool)
            'destination': 'target_directory',
            'dest': 'target_directory',
            'target_dir': 'target_directory',
            'output_dir': 'target_directory',
            'clone_dir': 'target_directory',
            
            # Maven/build specific (non-conflicting)
            'options': 'properties',
            'opts': 'properties',
            'maven_options': 'properties',
            'build_options': 'properties',
            
            # Context specific
            'context_type': 'action',
            'name': 'task_id',
            'parameters': 'summary',
            'task_name': 'task_id',
            'id': 'task_id',
            
            # Content variations (file_io tool)
            'data': 'content',
            'text': 'content',
            'body': 'content',
            'file_content': 'content',
        }
        
        # Tool-specific mappings for better accuracy
        tool_specific_mappings = {
            'bash': {
                'cmd': 'command',
                'script': 'command',
                'exec': 'command',
                'shell': 'command',
                'run': 'command',
                'execute': 'command',
                'bash_command': 'command',
                'shell_command': 'command',
                'dir': 'working_directory',
                'cwd': 'working_directory',
                'working_dir': 'working_directory',
                'workdir': 'working_directory',  # Map old workdir to working_directory
                'work_dir': 'working_directory',
                'directory': 'working_directory',
                'path': 'working_directory',  # Path should also map to working_directory for bash
            },
            'file_io': {
                'file': 'path',
                'filename': 'path',
                'filepath': 'path',
                'file_path': 'path',
                'operation': 'action',
                'op': 'action',
                'data': 'content',
                'text': 'content',
            },
            'project_setup': {
                'url': 'repository_url',
                'repo': 'repository_url',
                'destination': 'target_directory',
                'dest': 'target_directory',
                'output': 'target_directory',
            },
            'maven': {
                'goals': 'command',
                'options': 'properties',
                'dir': 'working_directory',
                'project_dir': 'working_directory',
                'cmd': 'command',  # Common mistake
                'maven_command': 'command',
            },
            'manage_context': {
                'type': 'action',
                'operation': 'action',
                'context_type': 'action',
                'name': 'task_id',
                'id': 'task_id',
                'target': 'action',  # Map target to action for switch-like operations
                'switch': 'action',  # Map switch to action
                'task_name': 'task_id',
                'branch_name': 'task_id',
                # CRITICAL FIX: Map content-related parameters to 'entry' for add_context action
                'description': 'entry',  # Fixed: was incorrectly mapped to 'summary'
                'content': 'entry',
                'data': 'entry',
                'info': 'entry',
                'details': 'entry',
                'context': 'entry',
                'observation': 'entry',
                'result': 'entry',
                # For complete_task action, these should map to summary  
                'completion_summary': 'summary',
                'task_summary': 'summary',
                'results': 'summary',
            }
        }
        
        # Apply tool-specific mappings first (higher priority)
        if tool_name in tool_specific_mappings:
            tool_mappings = tool_specific_mappings[tool_name]
            for old_name, new_name in tool_mappings.items():
                if old_name in fixed_params and new_name in properties:
                    # If target parameter exists but old parameter has a non-default value, use the old value
                    if new_name in fixed_params:
                        # Check if the existing value is a default/placeholder value
                        existing_value = fixed_params[new_name]
                        old_value = fixed_params[old_name]
                        if (existing_value in ['help', '', None] or 
                            str(existing_value).strip() == '' or
                            (isinstance(existing_value, str) and len(old_value) > len(existing_value))):
                            fixed_params[new_name] = old_value
                            logger.info(f"🔧 Tool-specific rename (override): '{old_name}' → '{new_name}' for {tool_name}")
                        else:
                            logger.debug(f"🔧 Skipping rename '{old_name}' → '{new_name}' (target has value: {existing_value})")
                    else:
                        # Target doesn't exist, normal mapping
                        fixed_params[new_name] = fixed_params[old_name]
                        logger.info(f"🔧 Tool-specific rename: '{old_name}' → '{new_name}' for {tool_name}")
                    
                    # Always delete the old parameter
                    del fixed_params[old_name]
        
        # Apply general mappings if target parameter exists in schema
        mappings_applied = []
        for old_name, new_name in name_mappings.items():
            if old_name in fixed_params and new_name in properties and new_name not in fixed_params:
                # Extract value from nested structure if needed (fix for parameters->summary mapping issue)
                old_value = fixed_params[old_name]
                if isinstance(old_value, dict) and len(old_value) == 1 and new_name in old_value:
                    # Handle case where we have {'summary': {'summary': '...'}} -> extract the inner value
                    fixed_params[new_name] = old_value[new_name]
                    logger.info(f"🔧 Extracted nested value from '{old_name}' to '{new_name}' for {tool_name}")
                else:
                    fixed_params[new_name] = old_value
                    logger.info(f"🔧 Renamed parameter '{old_name}' to '{new_name}' for {tool_name}")
                
                del fixed_params[old_name]
                mappings_applied.append(f"{old_name} → {new_name}")
        
        # Log all mappings applied for debugging
        if mappings_applied:
            logger.debug(f"Parameter mappings applied for {tool_name}: {', '.join(mappings_applied)}")
        
        return fixed_params

    def _update_successful_states(self, tool_name: str, params: Dict[str, Any], result: ToolResult):
        """Update successful states based on tool execution results."""
        try:
            # CRITICAL FIX: Reset context switch counter when context actually switches
            if tool_name == "manage_context" and result.success:
                action = params.get("action", "")
                if action in ["start_task", "complete_with_results", "complete_task", "switch_to_trunk"]:
                    # Reset the counter when we switch contexts
                    self.steps_since_context_switch = 0
                    logger.info(f"✅ Reset steps_since_context_switch counter after {action}")
            
            if tool_name == "bash":
                # CRITICAL FIX: Get actual working directory from tool result metadata
                # This handles cases where bash tool had to fall back to alternative directories
                actual_working_dir = None
                
                # First try to get the actual working directory from metadata
                if hasattr(result, 'metadata') and result.metadata:
                    actual_working_dir = result.metadata.get('working_directory')
                
                # Fallback to parameter if metadata not available
                if not actual_working_dir:
                    actual_working_dir = params.get("working_directory")
                
                if actual_working_dir:
                    # Check if working directory changed (fallback occurred)
                    original_dir = params.get("working_directory", "/workspace")
                    if actual_working_dir != original_dir:
                        # PRIORITY CHECK: Is this a workspace-related fallback?
                        if original_dir.startswith("/workspace") and not actual_working_dir.startswith("/workspace"):
                            logger.error(f"🚨 WORKSPACE FALLBACK: Failed to use {original_dir}, fell back to {actual_working_dir}")
                            logger.error(f"🚨 This is a MAJOR ISSUE - projects should be in /workspace")
                            logger.error(f"🚨 Clone operations may not work correctly in {actual_working_dir}")
                            
                            # Mark this as an abnormal state
                            self.successful_states['workspace_fallback'] = True
                            self.successful_states['fallback_reason'] = f"Could not establish {original_dir}"
                        else:
                            logger.warning(f"🔧 Working directory change: {original_dir} → {actual_working_dir}")
                        
                        # CRITICAL: Update all related tools to use the new working directory
                        self._propagate_working_directory_change(actual_working_dir, original_dir)
                    else:
                        # Normal operation - workspace is working correctly
                        if actual_working_dir.startswith("/workspace"):
                            logger.debug(f"✅ Workspace operation normal: {actual_working_dir}")
                            # Clear any previous fallback flags
                            self.successful_states.pop('workspace_fallback', None)
                            self.successful_states.pop('fallback_reason', None)
                    
                    self.successful_states['working_directory'] = actual_working_dir
                    logger.debug(f"Updated successful working directory: {actual_working_dir}")
            
            elif tool_name == "maven" and params.get("working_directory"):
                # Remember successful Maven working directory
                if "BUILD SUCCESS" in (result.output or ""):
                    # Get working_directory parameter (standardized across all tools)
                    maven_workdir = params.get('working_directory', '/workspace')
                    self.successful_states['working_directory'] = maven_workdir
                    self.successful_states['maven_success'] = True
                    
                    # Check if Maven is working outside workspace (concerning)
                    if not maven_workdir.startswith("/workspace"):
                        logger.warning(f"⚠️ Maven succeeded outside workspace: {maven_workdir}")
                        logger.warning(f"⚠️ This may indicate workspace issues")
                    else:
                        logger.info(f"✅ Maven success in workspace: {maven_workdir}")
                    
                    logger.info(f"Maven success recorded for directory: {maven_workdir}")
            
            elif tool_name == "project_setup":
                # Remember cloned repositories and project type
                if params.get("repository_url"):
                    self.successful_states['cloned_repos'].add(params['repository_url'])
                    logger.debug(f"Recorded cloned repo: {params['repository_url']}")
                    
                    # Set working directory based on cloned repository
                    if params.get("action") == "clone":
                        repo_name = params['repository_url'].split('/')[-1].replace('.git', '')
                        
                        # PRIORITY: Always try to clone in /workspace first
                        if self.successful_states.get('workspace_fallback'):
                            # We're in fallback mode - this is not ideal for cloning
                            current_workdir = self.successful_states.get('working_directory', '/root')
                            clone_dir = f"{current_workdir}/{repo_name}"
                            logger.error(f"🚨 CLONING IN FALLBACK LOCATION: {clone_dir}")
                            logger.error(f"🚨 This is SUBOPTIMAL - prefer /workspace for projects")
                        else:
                            # Normal case - clone in workspace
                            clone_dir = f"/workspace/{repo_name}"
                            logger.info(f"✅ Cloning in proper workspace location: {clone_dir}")
                        
                        self.successful_states['working_directory'] = clone_dir
                        logger.info(f"Updated working directory after clone: {clone_dir}")
                
                # Check for project type detection in output
                output = result.output or ""
                if "maven" in output.lower() or "pom.xml" in output.lower():
                    self.successful_states['project_type'] = 'maven'
                    logger.debug("Detected Maven project type")
                elif "gradle" in output.lower() or "build.gradle" in output.lower():
                    self.successful_states['project_type'] = 'gradle'
                    logger.debug("Detected Gradle project type")
        
        except Exception as e:
            logger.warning(f"Failed to update successful states: {e}")

    def _propagate_working_directory_change(self, new_workdir: str, old_workdir: str):
        """
        Propagate working directory changes to ensure consistency across all tools.
        
        When bash tool falls back to a different directory, we need to update
        Agent's understanding of where the project is located.
        """
        try:
            logger.info(f"📁 Propagating working directory change: {old_workdir} → {new_workdir}")
            
            # Update successful states
            self.successful_states['working_directory'] = new_workdir
            
            # PRIORITY CHECK: Warn about workspace fallbacks
            if old_workdir.startswith("/workspace") and not new_workdir.startswith("/workspace"):
                logger.error(f"🚨 WORKSPACE LOST: Propagating fallback from {old_workdir} to {new_workdir}")
                logger.error(f"🚨 Future clone operations will be affected")
                logger.error(f"🚨 Consider fixing the underlying workspace issue")
                
                # Mark this propagation as problematic
                self.successful_states['workspace_fallback'] = True
                self.successful_states['fallback_reason'] = f"Propagated from failed {old_workdir}"
            elif new_workdir.startswith("/workspace"):
                logger.info(f"✅ Workspace propagation successful: {new_workdir}")
                # Clear fallback flags if we're back in workspace
                self.successful_states.pop('workspace_fallback', None)
                self.successful_states.pop('fallback_reason', None)
            
            # If we have cloned repositories, we might need to adjust their paths
            if self.successful_states.get('cloned_repos'):
                logger.info(f"📁 Note: Cloned repositories may need path adjustment for new working directory")
                
                # If we're falling back from workspace, this is a major concern
                if self.successful_states.get('workspace_fallback'):
                    logger.error(f"🚨 CRITICAL: Cloned repositories were in workspace, now using {new_workdir}")
                    logger.error(f"🚨 Project files may be in /workspace but operations will run in {new_workdir}")
                
            # Log for debugging
            logger.debug(f"📁 Agent state updated - new working directory: {new_workdir}")
            logger.debug(f"📁 All future operations will use this directory unless explicitly overridden")
            
        except Exception as e:
            logger.error(f"Failed to propagate working directory change: {e}")

    def _apply_basic_parameter_fixes(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Apply basic parameter fixes when schema is not available."""
        fixed_params = params.copy()
        
        # Tool-specific basic fixes
        if tool_name == "maven":
            if not fixed_params.get("command"):
                fixed_params["command"] = "compile"
        elif tool_name == "bash":
            if not fixed_params.get("command"):
                fixed_params["command"] = "pwd"  # Safe default
        elif tool_name == "file_io":
            if not fixed_params.get("action"):
                fixed_params["action"] = "read"
            if not fixed_params.get("file_path") and fixed_params.get("action") == "read":
                fixed_params["file_path"] = "/workspace"
        elif tool_name == "manage_context":
            if not fixed_params.get("action"):
                fixed_params["action"] = "get_info"
        elif tool_name == "project_setup":
            if not fixed_params.get("action"):
                # If we have a repository URL, default to clone
                if self.repository_url:
                    fixed_params["action"] = "clone"
                    fixed_params["repository_url"] = self.repository_url
                else:
                    fixed_params["action"] = "detect_project_type"
        elif tool_name == "web_search":
            if not fixed_params.get("query"):
                fixed_params["query"] = "help"
        
        return fixed_params

    def _apply_tool_specific_fixes(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Apply tool-specific parameter fixes using state memory."""
        fixed_params = params.copy()
        
        if tool_name == "project_setup":
            # Auto-inject repository URL if available and action is clone
            if fixed_params.get("action") == "clone" and not fixed_params.get("repository_url"):
                if self.repository_url:
                    fixed_params["repository_url"] = self.repository_url
                    logger.info(f"🔧 Auto-injected repository URL: {self.repository_url}")
            
            # CRITICAL FIX: Handle target_directory correctly for workspace vs fallback modes
            if fixed_params.get("action") == "clone":
                # Check current workspace status
                is_fallback_mode = self.successful_states.get('workspace_fallback', False)
                current_workdir = self.successful_states.get('working_directory', "/workspace")
                
                if is_fallback_mode:
                    # We're in abnormal fallback mode - need to specify full path
                    fallback_reason = self.successful_states.get('fallback_reason', 'Unknown reason')
                    
                    logger.error(f"🚨 CLONE IN FALLBACK MODE: Using {current_workdir}")
                    logger.error(f"🚨 Reason: {fallback_reason}")
                    logger.error(f"🚨 This is SUBOPTIMAL - clone should happen in /workspace")
                    
                    # For fallback mode, we need to specify the full path
                    if not fixed_params.get("target_directory"):
                        # Extract project name from URL
                        repo_name = fixed_params.get("repository_url", "").split('/')[-1].replace('.git', '')
                        if repo_name:
                            fallback_target = f"{current_workdir}/{repo_name}"
                            fixed_params["target_directory"] = fallback_target
                            logger.error(f"🚨 Setting fallback clone target: {fallback_target}")
                        else:
                            # Use fallback directory as-is
                            fixed_params["target_directory"] = current_workdir
                            logger.error(f"🚨 Using fallback directory directly: {current_workdir}")
                else:
                    # Normal case - workspace is available
                    logger.info(f"✅ CLONE IN WORKSPACE: Standard workspace cloning")
                    
                    # CRITICAL FIX: Don't set target_directory to /workspace!
                    # Let project_setup tool auto-generate the project subdirectory name
                    if fixed_params.get("target_directory") == "/workspace":
                        # Remove the incorrect target_directory - let tool auto-generate
                        del fixed_params["target_directory"]
                        logger.info(f"🔧 Removed incorrect target_directory, will auto-generate project subdirectory")
                    elif not fixed_params.get("target_directory"):
                        # No target_directory specified - this is correct, tool will auto-generate
                        logger.info(f"✅ No target_directory specified - project_setup will create subdirectory")
                    else:
                        # Explicit target_directory specified
                        target_dir = fixed_params["target_directory"]
                        if not target_dir.startswith("/workspace/"):
                            logger.warning(f"⚠️ EXPLICIT NON-WORKSPACE CLONE: {target_dir}")
                            logger.warning(f"⚠️ This may cause project layout issues")
                        else:
                            logger.info(f"✅ Workspace subdirectory clone: {target_dir}")
            
            # Prevent duplicate cloning
            if (fixed_params.get("action") == "clone" and 
                fixed_params.get("repository_url") in self.successful_states['cloned_repos']):
                logger.warning(f"🔧 Repository already cloned, changing action to detect_project_type")
                fixed_params["action"] = "detect_project_type"
        
        elif tool_name == "maven":
            # Ensure maven has a valid command
            if not fixed_params.get("command") or fixed_params.get("command").strip() == "":
                # Use intelligent default based on current state
                if self.successful_states['maven_success']:
                    fixed_params["command"] = "test"  # If compile succeeded before, try test
                else:
                    fixed_params["command"] = "compile"  # Start with compile
            
            # Auto-inject successful working directory for Maven operations
            if "working_directory" not in fixed_params:
                if self.successful_states['working_directory']:
                    fixed_params["working_directory"] = self.successful_states['working_directory']
                    logger.info(f"🔧 Auto-injected successful working directory: {self.successful_states['working_directory']}")
                else:
                    # Try to infer from repository URL
                    if self.repository_url:
                        repo_name = self.repository_url.split('/')[-1].replace('.git', '')
                        fixed_params["working_directory"] = f"/workspace/{repo_name}"
                        logger.info(f"🔧 Inferred working directory from repo: /workspace/{repo_name}")
            
            # Convert common typos
            command = fixed_params.get("command", "")
            if command in ["test", "tests"]:
                fixed_params["command"] = "test"
            elif command in ["build", "compile"]:
                fixed_params["command"] = "compile" 
            elif command in ["install", "package"]:
                fixed_params["command"] = "package"
                
        elif tool_name == "bash":
            # Ensure bash has a command
            if not fixed_params.get("command") or fixed_params.get("command").strip() == "":
                fixed_params["command"] = "pwd"
            
            # Auto-inject successful working directory for bash operations
            if "working_directory" not in fixed_params:
                if self.successful_states['working_directory']:
                    fixed_params["working_directory"] = self.successful_states['working_directory']
                    logger.info(f"🔧 Auto-injected successful working directory: {self.successful_states['working_directory']}")
                else:
                    fixed_params["working_directory"] = "/workspace"
                
        elif tool_name == "file_io":
            # Ensure file_io has an action
            if not fixed_params.get("action"):
                fixed_params["action"] = "read"
                
            # CRITICAL PRIORITY: Use workspace paths when possible, warn about fallbacks
            current_workdir = self.successful_states.get('working_directory', "/workspace")
            is_fallback_mode = self.successful_states.get('workspace_fallback', False)
            
            # If reading but no file path, default to current directory listing
            if fixed_params.get("action") == "read" and not fixed_params.get("path"):
                fixed_params["action"] = "list"
                fixed_params["path"] = current_workdir
                
                if is_fallback_mode:
                    logger.error(f"🚨 FILE_IO FALLBACK: Listing {current_workdir} (not in workspace)")
                else:
                    logger.info(f"✅ FILE_IO WORKSPACE: Listing {current_workdir}")
            
            # If path is relative and we have a known working directory, make it absolute
            elif fixed_params.get("path") and not fixed_params["path"].startswith('/'):
                relative_path = fixed_params["path"]
                absolute_path = f"{current_workdir}/{relative_path}"
                fixed_params["path"] = absolute_path
                
                if is_fallback_mode:
                    logger.error(f"🚨 FILE_IO FALLBACK PATH: {relative_path} → {absolute_path} (not in workspace)")
                else:
                    logger.info(f"✅ FILE_IO WORKSPACE PATH: {relative_path} → {absolute_path}")
            
            # PRIORITY CHECK: If path points to /workspace but we're in fallback mode, this is concerning
            elif fixed_params.get("path") and fixed_params["path"].startswith("/workspace"):
                if is_fallback_mode and not current_workdir.startswith("/workspace"):
                    original_path = fixed_params["path"]
                    logger.error(f"🚨 FILE_IO MISMATCH: Requesting {original_path} but workspace unavailable")
                    logger.error(f"🚨 Current fallback directory: {current_workdir}")
                    
                    # Try to map /workspace/... to current_workdir/... 
                    relative_part = original_path.replace("/workspace", "").lstrip("/")
                    if relative_part:
                        adjusted_path = f"{current_workdir}/{relative_part}"
                        logger.error(f"🚨 ATTEMPTING PATH MAPPING: {original_path} → {adjusted_path}")
                        logger.error(f"🚨 This may fail if files are actually in /workspace")
                    else:
                        adjusted_path = current_workdir
                        logger.error(f"🚨 MAPPING WORKSPACE ROOT to fallback: {adjusted_path}")
                    
                    fixed_params["path"] = adjusted_path
                else:
                    # Normal case - workspace path and we're in workspace
                    if not is_fallback_mode:
                        logger.debug(f"✅ FILE_IO WORKSPACE: Accessing {fixed_params['path']}")
                    else:
                        logger.info(f"✅ FILE_IO WORKSPACE: Accessing {fixed_params['path']} (workspace available)")
            
            # If we're in fallback mode, warn about any non-fallback paths
            elif is_fallback_mode and fixed_params.get("path"):
                path = fixed_params["path"]
                if not path.startswith(current_workdir):
                    logger.warning(f"⚠️ FILE_IO OUTSIDE FALLBACK: Accessing {path} while in fallback mode ({current_workdir})")
                    logger.warning(f"⚠️ This may fail if the path doesn't exist")
        
        elif tool_name == "manage_context":
            # Fix common action name errors with comprehensive alias mapping
            action = fixed_params.get("action", "")
            
            # Map common variations to correct actions
            action_aliases = {
                # Start task aliases
                "start": "start_task",
                "begin": "start_task",
                "create": "start_task",
                "create_branch": "start_task",
                "new": "start_task",
                "new_task": "start_task",
                
                # Get info aliases
                "info": "get_info",
                "status": "get_info",
                "current": "get_info",
                "check": "get_info",
                
                # Complete task aliases
                "complete": "complete_task",
                "finish": "complete_task",
                "end": "complete_task",
                "done": "complete_task",
                "complete_branch": "complete_task",
                "switch_to_trunk": "complete_task",
                "failure": "complete_task",
                "failed": "complete_task",
                
                # Add context aliases
                "add": "add_context",
                "record": "add_context",
                "log": "add_context",
                
                # Get context aliases
                "get": "get_full_context",
                "show": "get_full_context",
                "view": "get_full_context",
                "history": "get_full_context",
                
                # Compact context aliases
                "compress": "compact_context",
                "compact": "compact_context",
                "reduce": "compact_context"
            }
            
            if action in action_aliases:
                original_action = action
                fixed_params["action"] = action_aliases[action]
                logger.info(f"🔧 Converted action '{original_action}' to '{action_aliases[action]}' for manage_context")
                
                # Add default summary for completion actions
                if action_aliases[action] == "complete_task" and not fixed_params.get("summary"):
                    if action in ["failure", "failed"]:
                        fixed_params["summary"] = "Task failed to complete successfully due to encountered issues"
                    else:
                        fixed_params["summary"] = "Task completed with mixed results"
                    logger.info(f"🔧 Added default summary for complete_task action")
            elif action == "switch_to_trunk":
                # This is correct, but ensure we have a summary if needed
                if not fixed_params.get("summary"):
                    fixed_params["summary"] = "Switching back to trunk context"
                    logger.info(f"🔧 Added default summary for switch_to_trunk action")
            
            # Ensure required parameters for create_branch
            if fixed_params.get("action") == "create_branch":
                if not fixed_params.get("task_id"):
                    # Generate a default task_id if missing
                    summary = fixed_params.get("summary", "default_task")
                    task_id = summary.replace(" ", "_").lower()[:20]
                    fixed_params["task_id"] = task_id
                    logger.info(f"🔧 Generated missing task_id: {task_id}")
            
            # For start_task, ensure we have task_id
            elif fixed_params.get("action") == "start_task":
                if not fixed_params.get("task_id"):
                    # Auto-inject the correct next task ID based on context
                    fixed_params["task_id"] = "task_1"  # Default to first task
                    logger.info("🔧 Auto-injected default task_id: task_1 for start_task")
                    
            # For complete_task, ensure we have summary
            elif fixed_params.get("action") == "complete_task":
                if not fixed_params.get("summary"):
                    fixed_params["summary"] = "Task completed with mixed results"
                    logger.info(f"🔧 Added default summary for complete_task action")
        
        return fixed_params

    def _format_tool_result(self, tool_name: str, result: ToolResult) -> str:
        """Format tool result for observation. Output truncation is now handled in BaseTool."""
        if result.success:
            # For successful results, preserve key status information
            formatted = f"✅ {tool_name} executed successfully"
            
            # Add command information for bash tool
            if tool_name == "bash" and result.metadata and "command" in result.metadata:
                formatted += f"\nCommand: {result.metadata['command']}"
            
            # Add output (already processed by BaseTool truncation)
            if result.output:
                formatted += f"\n\nOutput: {result.output}"
            
            # Add metadata if available
            if result.metadata:
                if "exit_code" in result.metadata:
                    formatted += f"\nExit code: {result.metadata['exit_code']}"
                if "auto_installed" in result.metadata:
                    formatted += f"\nAuto-installed: {result.metadata['auto_installed']}"
                # Show truncation info if applicable
                if result.metadata.get("output_truncated"):
                    original_len = result.metadata.get("original_length", 0)
                    truncated_len = result.metadata.get("truncated_length", 0)
                    formatted += f"\n📏 Output truncated: {original_len} → {truncated_len} chars"
                    
        else:
            # For failed results, show error and suggestions
            error_msg = result.error if result.error else "Unknown error occurred"
            formatted = f"❌ {tool_name} failed: {error_msg}"
            
            # Add command information for failed bash tool
            if tool_name == "bash" and result.metadata and "command" in result.metadata:
                formatted += f"\nCommand: {result.metadata['command']}"
            
            # Show extracted error details from output (especially important for maven tool)
            if result.output and result.output.strip():
                formatted += f"\n\n{result.output}"
            
            if result.suggestions:
                formatted += f"\n\nSuggestions:\n" + "\n".join(f"• {s}" for s in result.suggestions[:3])
                
            if result.error_code:
                formatted += f"\nError code: {result.error_code}"
            
            # Add full raw output if available and error message is unclear (and no specific output was provided)
            if result.raw_output and (not result.error or len(result.error.strip()) < 10) and (not result.output or len(result.output.strip()) < 20):
                formatted += f"\n\nRaw output: {result.raw_output}"
                
        return formatted



    def _get_repetition_level(self, tool_signature: str) -> int:
        """
        Get the level of repetition for graduated response.
        Returns:
            0: No repetition concern
            1: Warning level (3 repetitions)
            2: Guidance level (4 repetitions)
            3: Force break level (5+ repetitions)
        """
        # Count exact matches
        exact_match_count = sum(1 for exec_info in self.recent_tool_executions 
                               if exec_info["signature"] == tool_signature)
        
        # Extract tool name
        tool_name = tool_signature.split(':')[0]
        
        # Special handling for manage_context
        if tool_name == "manage_context":
            if "start_task" in tool_signature or "get_info" in tool_signature:
                return 0  # Never block these
            if "complete_with_results" in tool_signature:
                if exact_match_count >= 6:
                    return 3
                elif exact_match_count >= 5:
                    return 2
                elif exact_match_count >= 4:
                    return 1
                return 0
        
        # Count tool executions
        tool_executions = [exec_info for exec_info in self.recent_tool_executions 
                          if exec_info["signature"].startswith(tool_name + ':')]
        tool_count = len(tool_executions)
        
        # Determine level based on counts
        if exact_match_count >= 5 or tool_count >= 8:
            return 3  # Force break
        elif exact_match_count >= 4 or tool_count >= 6:
            return 2  # Guidance
        elif exact_match_count >= 3 or tool_count >= 5:
            return 1  # Warning
        
        return 0  # No concern
    
    def _generate_alternative_suggestions(self, tool_name: str, params: Dict, recent_executions: List) -> str:
        """Generate context-aware alternative suggestions."""
        suggestions = []
        
        if tool_name == "bash":
            # Check for common bash issues
            if any("update-alternatives" in str(e) for e in recent_executions):
                suggestions.append("Use system tool's install_java action instead of manual update-alternatives")
            if any("java" in str(e) for e in recent_executions):
                suggestions.append("Try: system(action='verify_java') to check current Java version")
            suggestions.append("Use file_io tool to examine files before executing commands")
            
        elif tool_name == "maven":
            suggestions.append("Try: bash(command='mvn --version') to verify Maven installation")
            suggestions.append("Check pom.xml exists: file_io(action='read', file_path='pom.xml')")
            suggestions.append("Use bash tool for manual investigation: bash(command='ls -la')")
            
        elif tool_name == "system":
            if params.get("action") == "install_java":
                suggestions.append("Java might already be installed - verify with: system(action='verify_java')")
                suggestions.append("Check available Java versions: bash(command='ls /usr/lib/jvm/')")
                
        return "\n• ".join(suggestions) if suggestions else "Try a different tool or approach"
    
    def _auto_fix_java_configuration(self) -> ToolResult:
        """Automatically fix Java configuration issues."""
        logger.info("Attempting automatic Java configuration fix")
        
        # Use the system tool which now has proper architecture detection
        if "system" in self.tools:
            # First verify what Java is needed
            verify_result = self.tools["system"].execute(action="verify_java")
            
            # Check if Java 17 is needed (common for Tika)
            if "17" in str(self.context_manager.get_current_context()):
                logger.info("Detected Java 17 requirement, using system tool for proper installation")
                install_result = self.tools["system"].execute(action="install_java", java_version="17")
                
                if install_result.success:
                    return ToolResult(
                        success=True,
                        output="✅ Auto-fixed Java configuration using enhanced system tool\n" + install_result.output,
                        metadata={"auto_fixed": True, "java_version": "17"}
                    )
        
        # Fallback response
        return ToolResult(
            success=False,
            output="Could not auto-fix Java configuration. Skipping to next task.",
            error="Auto-fix failed",
            error_code="AUTO_FIX_FAILED",
            suggestions=["Manual intervention may be required", "Check Java installation logs"]
        )
    
    def _is_repetitive_execution(self, tool_signature: str) -> bool:
        """Check if this tool execution is repetitive."""
        # Count recent executions of the same tool with same parameters
        exact_match_count = sum(1 for exec_info in self.recent_tool_executions 
                               if exec_info["signature"] == tool_signature)
        
        # Extract tool name from signature
        tool_name = tool_signature.split(':')[0]
        
        # Special handling for manage_context actions
        if tool_name == "manage_context":
            # Never block start_task or get_info - these should always be allowed
            if "start_task" in tool_signature or "get_info" in tool_signature:
                return False
            # Allow more retries for task completion since agent often needs to correct parameters
            if "complete_with_results" in tool_signature:
                return exact_match_count >= 4
        
        # Count recent executions of the same tool (regardless of parameters)
        tool_executions = [exec_info for exec_info in self.recent_tool_executions 
                          if exec_info["signature"].startswith(tool_name + ':')]
        
        # Check for patterns that indicate repetitive execution
        recent_tool_count = len(tool_executions)
        recent_failures = sum(1 for exec_info in tool_executions if not exec_info["success"])
        
        # Stricter thresholds to catch failures earlier and prevent stuck states
        # Block if: 
        # 1. Exact same call attempted 2+ times (reduced from 3), OR
        # 2. Same tool failed 3+ times recently (reduced from 5), OR  
        # 3. Same tool called 5+ times in recent executions (reduced from 8)
        return (exact_match_count >= 2 or 
                recent_failures >= 3 or 
                recent_tool_count >= 5)

    def _track_tool_execution(self, tool_signature: str, success: bool):
        """Track tool execution to detect repetitive patterns."""
        execution_info = {
            "signature": tool_signature,
            "success": success,
            "timestamp": self._get_timestamp()
        }
        
        self.recent_tool_executions.append(execution_info)
        
        # Keep only recent executions to prevent memory bloat
        if len(self.recent_tool_executions) > self.max_recent_executions:
            self.recent_tool_executions.pop(0)

    def _add_observation_step(self, observation: str):
        """Add an observation step, enriched with physical validation state."""
        # Get physical validation state if relevant
        physical_state = self._get_physical_validation_state(observation)
        
        # Enrich observation with physical state if available
        if physical_state:
            observation = self._enrich_observation_with_physical_state(observation, physical_state)
        
        obs_step = ReActStep(
            step_type=StepType.OBSERVATION, content=observation, timestamp=self._get_timestamp()
        )
        self.steps.append(obs_step)

        # FIXED: Only log once to prevent duplicate output in logs
        # Use logger.info for main logging, agent_logger for internal tracking only
        logger.info(f"👁️ OBSERVATION: {observation}")
        
        # DEPRECATED: Task completion detection now handled by state_evaluator
        # self._check_task_completion_opportunity(observation)

    def _is_task_complete(self) -> bool:
        """Check if the current task is complete."""
        # Check for report tool completion signal (highest priority)
        recent_steps = self.steps[-3:] if len(self.steps) >= 3 else self.steps
        
        for step in recent_steps:
            if step.step_type == StepType.ACTION and step.tool_name == "report":
                if step.tool_result and step.tool_result.success:
                    metadata = step.tool_result.metadata or {}
                    if metadata.get("completion_signal") or metadata.get("task_completed"):
                        logger.info("Task completion detected via report tool")
                        return True
        
        # Check for successful Maven test completion (rule-based completion)
        if self._check_maven_completion():
            logger.info("Task completion detected via Maven success criteria")
            return True
        
        # Look at recent steps for EXPLICIT completion indicators
        recent_steps = self.steps[-5:] if len(self.steps) >= 5 else self.steps

        for step in recent_steps:
            if step.step_type == StepType.THOUGHT:
                content_lower = step.content.lower()
                # Only consider overall completion phrases, not individual task completion
                if any(
                    phrase in content_lower
                    for phrase in [
                        "all tasks completed",
                        "project setup complete", 
                        "setup finished",
                        "build and test complete",
                        "maven build successful and report generated",
                        "final report completed"
                    ]
                ):
                    logger.info(f"Task completion detected via thought: {step.content[:100]}...")
                    return True

            elif step.step_type == StepType.ACTION and step.tool_name == "manage_context":
                # Only consider trunk context operations that indicate ALL tasks are done
                if step.tool_params and step.tool_params.get("action") == "complete_task":
                    # Check if this was the completion of the LAST task
                    if step.tool_result and step.tool_result.success and step.tool_result.metadata:
                        metadata = step.tool_result.metadata
                        # Only complete if there are no more tasks OR if explicit completion signal
                        if (metadata.get("all_tasks_completed") or 
                            not metadata.get("next_task")):
                            logger.info("Task completion detected: all TODO tasks completed")
                            return True
                
                # Don't treat individual branch task completion as overall completion
                # The agent should continue with the next task in the TODO list

        return False

    def _check_maven_completion(self) -> bool:
        """Check if Maven project has been successfully built and tested."""
        # Look for successful Maven test execution in recent steps
        recent_steps = self.steps[-10:] if len(self.steps) >= 10 else self.steps
        
        maven_compile_success = False
        maven_test_success = False
        
        for step in recent_steps:
            if (step.step_type == StepType.ACTION and 
                step.tool_name == "maven" and 
                step.tool_result and step.tool_result.success):
                
                output = step.tool_result.output or ""
                command = step.tool_params.get("command", "") if step.tool_params else ""
                
                # Check for successful compilation
                if ("compile" in command.lower() and 
                    "BUILD SUCCESS" in output):
                    maven_compile_success = True
                    logger.debug("Maven compile success detected")
                
                # Check for successful test execution
                if ("test" in command.lower() and 
                    "BUILD SUCCESS" in output and
                    "Tests run:" in output):
                    
                    # Parse test results
                    import re
                    test_match = re.search(r'Tests run: (\d+), Failures: (\d+), Errors: (\d+)', output)
                    if test_match:
                        total, failures, errors = map(int, test_match.groups())
                        if failures == 0 and errors == 0 and total > 0:
                            maven_test_success = True
                            logger.info(f"Maven test success detected: {total} tests, 0 failures, 0 errors")
        
        # Consider task complete if test succeeded (test usually includes compilation)
        # OR if both compile and test succeeded explicitly
        if maven_test_success or (maven_compile_success and maven_test_success):
            logger.info("Maven project completion criteria met: test successful")
            # Add completion guidance for the agent
            self._add_completion_guidance("Maven build and test completed successfully")
            return True
            
        return False

    def _add_completion_guidance(self, reason: str):
        """Add guidance to help agent recognize task completion."""
        guidance = (f"SYSTEM GUIDANCE: Task completion detected! {reason}. "
                   f"You should now generate a completion report using the report tool "
                   f"with a summary of what was accomplished, then the system will stop.")
        
        guidance_step = ReActStep(
            step_type=StepType.SYSTEM_GUIDANCE,
            content=guidance,
            timestamp=self._get_timestamp(),
        )
        self.steps.append(guidance_step)
        
        self.agent_logger.info(f"🏁 COMPLETION GUIDANCE: {guidance}")
        logger.info(f"🏁 COMPLETION GUIDANCE: Task completion detected - {reason}")

    def _check_completion_suggestion(self) -> str:
        """Check if we should strongly suggest task completion."""
        # Check if Maven build and test succeeded but no report generated yet
        if (self.successful_states['maven_success'] and 
            not self._has_report_been_generated()):
            
            # Look for recent Maven test success
            recent_steps = self.steps[-10:] if len(self.steps) >= 10 else self.steps
            for step in recent_steps:
                if (step.step_type == StepType.ACTION and 
                    step.tool_name == "maven" and 
                    step.tool_result and step.tool_result.success):
                    
                    output = step.tool_result.output or ""
                    if ("test" in step.tool_params.get("command", "").lower() and
                        "BUILD SUCCESS" in output and
                        "Tests run:" in output):
                        
                        # Parse test results to confirm no failures
                        import re
                        test_match = re.search(r'Tests run: (\d+), Failures: (\d+), Errors: (\d+)', output)
                        if test_match:
                            total, failures, errors = map(int, test_match.groups())
                            if failures == 0 and errors == 0 and total > 0:
                                return f"Maven build and test completed successfully ({total} tests passed)"
        
        # Check if we've been running for many iterations without progress
        if self.current_iteration >= 25 and not self._has_report_been_generated():
            # Check if we have any clear successes
            if self.successful_states['cloned_repos'] or self.successful_states['maven_success']:
                return "Task has been running for many iterations with some successes"
        
        return None
    
    def _has_report_been_generated(self) -> bool:
        """Check if a report has already been generated."""
        for step in self.steps:
            if (step.step_type == StepType.ACTION and 
                step.tool_name == "report" and 
                step.tool_result and step.tool_result.success):
                return True
        return False
    
    @deprecated("This is no longer used")
    def _add_strong_completion_guidance(self, reason: str):
        """Add strong guidance to push agent toward completion."""
        guidance = (f"🚨 URGENT COMPLETION NOTICE: {reason}. "
                   f"You MUST now call the report tool to generate a completion summary. "
                   f"Example: report(action='generate', "
                   f"summary='Maven project successfully built and tested'). "
                   f"This will complete the task and stop further iterations.")
        
        guidance_step = ReActStep(
            step_type=StepType.SYSTEM_GUIDANCE,
            content=guidance,
            timestamp=self._get_timestamp(),
        )
        self.steps.append(guidance_step)
        
        self.agent_logger.info(f"🚨 URGENT COMPLETION: {guidance}")
        logger.info(f"🚨 URGENT COMPLETION: Strong completion guidance added - {reason}")

    @deprecated("This is no longer used")
    def _check_context_switching_guidance(self):
        """Check if we should provide context switching guidance."""
        if self.steps_since_context_switch >= self.context_switch_threshold:
            # Check if we're in a branch context and haven't switched recently
            if self.context_manager.current_task_id:
                guidance = (
                    f"SYSTEM GUIDANCE: You have been working on the current task for "
                    f"{self.steps_since_context_switch} steps. Consider if the sub-task "
                    f"is complete and if you should return to the trunk context with a summary "
                    f"using the manage_context tool."
                )

                guidance_step = ReActStep(
                    step_type=StepType.SYSTEM_GUIDANCE,
                    content=guidance,
                    timestamp=self._get_timestamp(),
                )
                self.steps.append(guidance_step)

                self.agent_logger.info(f"🔔 SYSTEM GUIDANCE: {guidance}")
                logger.info(f"🔔 SYSTEM GUIDANCE: Context switch suggestion")

                # Reset counter
                self.steps_since_context_switch = 0

    def _build_next_prompt(self) -> str:
        """Build the prompt for the next iteration."""
        prompt = "CONVERSATION HISTORY:\n\n"

        # Limit recent steps to avoid context window overflow
        # Keep the most recent steps, but cap the total length
        max_steps = 7  # Start with fewer steps to stay within context window
        
        # If we have more steps, take the first few and the most recent ones
        if len(self.steps) > max_steps * 2:
            # Take first 2 steps (usually context and first action) and last max_steps
            recent_steps = self.steps[:2] + self.steps[-max_steps:]
            prompt += "... (earlier steps omitted for brevity) ...\n\n"
        elif len(self.steps) > max_steps:
            # Just take the most recent steps
            recent_steps = self.steps[-max_steps:]
        else:
            recent_steps = self.steps

        for step in recent_steps:
            if step.step_type == StepType.THOUGHT:
                # Truncate very long thoughts to keep context manageable
                content = step.content[:5000] + "..." if len(step.content) > 5000 else step.content
                prompt += f"THOUGHT: {content}\n\n"
            elif step.step_type == StepType.ACTION:
                prompt += f"ACTION: {step.tool_name}\n"
                if step.tool_params:
                    prompt += f"PARAMETERS: {json.dumps(step.tool_params)}\n\n"
            elif step.step_type == StepType.OBSERVATION:
                # Truncate very long observations to keep context manageable
                content = step.content[:5000] + "..." if len(step.content) > 5000 else step.content
                prompt += f"OBSERVATION: {content}\n\n"
            elif step.step_type == StepType.SYSTEM_GUIDANCE:
                prompt += f"SYSTEM GUIDANCE: {step.content}\n\n"

        # Check if we need to provide format guidance
        thoughts_without_actions = 0
        for step in reversed(recent_steps):
            if step.step_type == StepType.THOUGHT:
                thoughts_without_actions += 1
            elif step.step_type == StepType.ACTION:
                break
        
        if thoughts_without_actions >= 3:
            # Model seems stuck in thinking without acting
            if self.supports_function_calling:
                prompt += """
IMPORTANT: You have been thinking without taking action. Please use the available tools to make progress. 
Use function calling to execute actions. Here's a reminder of available tools:
- project_setup: Clone repositories and setup projects
- manage_context: Manage context switching
- bash: Execute shell commands
- file_io: Read and write files
- maven: Execute Maven commands
- web_search: Search the web for information
- system: Install system packages

"""
                # Add specific guidance based on repository URL
                if self.repository_url:
                    prompt += f"""The repository URL is already set: {self.repository_url}

USE CORRECT TASK MANAGEMENT FLOW:
1. FIRST check context status:
   Call manage_context with action="get_info"
   
2. THEN start the first task:
   Call manage_context with action="start_task" and task_id="task_1"
   
3. IN TASK CONTEXT, clone the repository:
   Call project_setup with action="clone" and repository_url="{self.repository_url}"
   
4. IMMEDIATELY after clone success:
   Call project_analyzer with action="analyze"

🔄 CORRECT CONTINUOUS FLOW: 
1. manage_context(action="get_info") - Check TODO list
2. manage_context(action="start_task", task_id="...") - Start task  
3. [Do the work: project_setup, project_analyzer, maven, etc.]
4. manage_context(action="complete_with_results", summary="...", key_results="...") - Complete task
5. GO BACK TO SEQUENCE STEP 1 - Don't skip this! Check for next task

⚠️ CRITICAL: After completing ANY task, ALWAYS go back to sequence step 1 (get_info) before doing more work!
"""
            else:
                prompt += """
IMPORTANT: You must take ACTION now. Use this format:

ACTION: [tool_name]
PARAMETERS: {"param1": "value1", "param2": "value2"}

For example:
ACTION: project_setup
PARAMETERS: {"action": "clone", "repository_url": "...", "directory": "/workspace"}

"""

        prompt += "Continue with your next THOUGHT and ACTION:\n\n"
        
        # Apply memory protection to prevent critical info loss due to context pollution
        prompt = self._inject_memory_protection(prompt)
        
        return prompt

    def _get_timestamp(self) -> str:
        """Get current timestamp string."""
        from datetime import datetime

        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @deprecated("This is no longer used")
    def _needs_action_guidance(self) -> bool:
        """Check if the model needs explicit action guidance."""
        # Count recent thoughts without actions
        recent_steps = self.steps[-5:] if len(self.steps) >= 5 else self.steps
        thoughts_count = 0
        actions_count = 0
        
        for step in reversed(recent_steps):
            if step.step_type == StepType.THOUGHT:
                thoughts_count += 1
                # Check if the thought mentions needing repository URL
                if any(phrase in step.content.lower() for phrase in [
                    "need the repository url",
                    "need access to",
                    "please share",
                    "could you",
                    "waiting for",
                    "require the url"
                ]):
                    return True
            elif step.step_type == StepType.ACTION:
                actions_count += 1
                
        # Need guidance if too many thoughts without actions
        return thoughts_count >= 3 and actions_count == 0

    @deprecated("This is no longer used")
    def _add_action_guidance(self):
        """Add explicit action guidance to help the model."""
        if self.repository_url:
            guidance = f"""SYSTEM GUIDANCE: You seem to be stuck. The repository URL is: {self.repository_url}

Take ACTION NOW using function calling. Here's exactly what to do:

Option 1: Clone the repository immediately:
- Use project_setup tool with action="clone" and repository_url="{self.repository_url}"

Option 2: Check context first:
- Use manage_context tool with action="get_info"

Option 3: If already cloned, navigate to the project:
- Use bash tool with command="cd /workspace && ls -la"

STOP asking for the repository URL - it's already provided above!"""
        else:
            guidance = """SYSTEM GUIDANCE: You need to take action. Use the available tools:

1. manage_context - Check your current context
2. bash - Execute shell commands
3. file_io - Read and write files
4. web_search - Search for information
5. maven - Run Maven commands
6. project_setup - Clone and setup projects
7. system - Install packages

Use function calling to execute these tools!"""

        guidance_step = ReActStep(
            step_type=StepType.SYSTEM_GUIDANCE,
            content=guidance,
            timestamp=self._get_timestamp(),
        )
        self.steps.append(guidance_step)
        
        self.agent_logger.info(f"🔔 SYSTEM GUIDANCE: Added explicit action guidance")
        logger.info(f"🔔 SYSTEM GUIDANCE: Model needs explicit action guidance")

    def _log_llm_request(
        self, model: str, prompt: str, temperature: float, max_tokens: int, is_thinking: bool
    ):
        """Log detailed LLM request in verbose mode."""

        verbose_logger = create_verbose_logger("react_llm")

        log_entry = {
            "event": "llm_request",
            "model": model,
            "model_type": "thinking" if is_thinking else "action",
            "iteration": self.current_iteration,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "prompt_length": len(prompt),
            "full_prompt": prompt,  # Show full prompt instead of preview
            "timestamp": self._get_timestamp(),
        }

        verbose_logger.info(f"🤖 LLM REQUEST: {json.dumps(log_entry, indent=2)}")

        # Also save full prompt to container file if we have access
        if hasattr(self.context_manager, "orchestrator") and self.context_manager.orchestrator:
            prompt_file = (
                f"/workspace/.setup_agent/llm_traces/iteration_{self.current_iteration}_request.txt"
            )
            escaped_prompt = prompt.replace("'", "'\"'\"'")
            self.context_manager.orchestrator.execute_command(
                f"mkdir -p /workspace/.setup_agent/llm_traces && echo '{escaped_prompt}' > {prompt_file}"
            )

    def _log_llm_response(self, model: str, content: str, response):
        """Log detailed LLM response in verbose mode."""

        verbose_logger = create_verbose_logger("react_llm")

        # Extract usage information if available
        usage_info = {}
        if hasattr(response, "usage") and response.usage:
            usage_info = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                "total_tokens": getattr(response.usage, "total_tokens", 0),
            }

        log_entry = {
            "event": "llm_response",
            "model": model,
            "iteration": self.current_iteration,
            "response_length": len(content),
            "full_response": content,  # Show full response instead of preview
            "usage": usage_info,
            "timestamp": self._get_timestamp(),
        }

        verbose_logger.info(f"🤖 LLM RESPONSE: {json.dumps(log_entry, indent=2)}")

        # Also save full response to container file if we have access
        if hasattr(self.context_manager, "orchestrator") and self.context_manager.orchestrator:
            response_file = f"/workspace/.setup_agent/llm_traces/iteration_{self.current_iteration}_response.txt"
            escaped_content = content.replace("'", "'\"'\"'")
            self.context_manager.orchestrator.execute_command(
                f"echo '{escaped_content}' > {response_file}"
            )

    def _log_llm_error(self, error: Exception):
        """Log LLM errors in verbose mode."""

        verbose_logger = create_verbose_logger("react_llm")

        error_entry = {
            "event": "llm_error",
            "iteration": self.current_iteration,
            "error_type": type(error).__name__,
            "error_message": str(error),
            "timestamp": self._get_timestamp(),
        }

        verbose_logger.error(f"🚨 LLM ERROR: {json.dumps(error_entry, indent=2)}")

    def _log_react_step_verbose(self, step: ReActStep):
        """Log detailed ReAct step information in verbose mode."""

        verbose_logger = create_verbose_logger("react_steps")

        step_entry = {
            "event": "react_step",
            "step_type": step.step_type,
            "iteration": self.current_iteration,
            "step_number": len(self.steps),
            "model_used": step.model_used,
            "content_length": len(step.content),
            "content": step.content,
            "tool_name": step.tool_name,
            "tool_params": step.tool_params,
            "timestamp": step.timestamp,
        }

        verbose_logger.info(f"📝 REACT STEP: {json.dumps(step_entry, indent=2, default=str)}")

    def _log_tool_execution_verbose(self, tool_name: str, params: dict):
        """Log detailed tool execution information in verbose mode."""

        verbose_logger = create_verbose_logger("react_tools")

        execution_entry = {
            "event": "tool_execution_start",
            "tool_name": tool_name,
            "iteration": self.current_iteration,
            "parameters": params,
            "timestamp": self._get_timestamp(),
        }

        verbose_logger.info(
            f"🔧 TOOL EXECUTION: {json.dumps(execution_entry, indent=2, default=str)}"
        )

    def _get_physical_validation_state(self, observation: str) -> Optional[Dict[str, any]]:
        """
        Get physical validation state for build/test related observations.
        
        Args:
            observation: The observation text
            
        Returns:
            Physical validation state dict or None
        """
        # Only validate for build/test related observations
        obs_lower = observation.lower()
        if not any(keyword in obs_lower for keyword in ['build', 'compile', 'test', 'maven', 'gradle', 'success', 'fail']):
            return None
        
        try:
            # Get project name from context or use default
            project_name = None
            if hasattr(self.context_manager, 'project_name'):
                project_name = self.context_manager.project_name
            
            # Run physical validation
            validation_result = self.physical_validator.validate_build_artifacts(project_name)
            
            # Check if we need to replay commands
            if 'build success' in obs_lower or 'build fail' in obs_lower:
                # Try to get the last build command from command tracker if available
                if hasattr(self, 'command_tracker') and self.command_tracker:
                    last_build = self.command_tracker.get_last_build_command()
                    if last_build:
                        replay_result = self.physical_validator.replay_last_build_command(
                            last_build['command'],
                            last_build.get('working_dir')
                        )
                        validation_result['build_replay'] = replay_result
            
            return validation_result
            
        except Exception as e:
            logger.warning(f"Physical validation failed: {e}")
            return None
    
    def _enrich_observation_with_physical_state(self, observation: str, physical_state: Dict[str, any]) -> str:
        """
        Enrich observation with physical validation facts.
        
        Args:
            observation: Original observation text
            physical_state: Physical validation state dict
            
        Returns:
            Enriched observation text
        """
        # Build physical evidence summary
        evidence_lines = []
        
        if physical_state.get('class_files', 0) > 0:
            evidence_lines.append(f"[PHYSICAL EVIDENCE: {physical_state['class_files']} .class files exist]")
        else:
            evidence_lines.append("[PHYSICAL EVIDENCE: No .class files found - compilation may have failed]")
        
        if physical_state.get('jar_files', 0) > 0:
            evidence_lines.append(f"[PHYSICAL EVIDENCE: {physical_state['jar_files']} JAR files exist]")
        
        if physical_state.get('missing_classes'):
            count = len(physical_state['missing_classes'])
            evidence_lines.append(f"[PHYSICAL EVIDENCE: {count} Java files have no corresponding .class files]")
        
        if 'build_replay' in physical_state:
            if physical_state['build_replay']:
                evidence_lines.append("[PHYSICAL EVIDENCE: Build command replay succeeded]")
            else:
                evidence_lines.append("[PHYSICAL EVIDENCE: Build command replay failed]")
        
        # Add evidence to observation
        if evidence_lines:
            return observation + "\n" + "\n".join(evidence_lines)
        
        return observation

    def _log_tool_result_verbose(self, tool_name: str, result):
        """Log detailed tool result information in verbose mode."""

        verbose_logger = create_verbose_logger("react_tools")

        result_entry = {
            "event": "tool_execution_result",
            "tool_name": tool_name,
            "iteration": self.current_iteration,
            "success": result.success,
            "output_length": len(result.output) if result.output else 0,
            "full_output": result.output,  # Show full output instead of preview
            "error": result.error if hasattr(result, "error") else None,
            "timestamp": self._get_timestamp(),
        }

        verbose_logger.info(f"🔧 TOOL RESULT: {json.dumps(result_entry, indent=2, default=str)}")

        # Save full tool output to container file if we have access
        if (
            hasattr(self.context_manager, "orchestrator")
            and self.context_manager.orchestrator
            and result.output
        ):
            output_file = f"/workspace/.setup_agent/tool_traces/iteration_{self.current_iteration}_{tool_name}_output.txt"
            escaped_output = result.output.replace("'", "'\"'\"'")
            self.context_manager.orchestrator.execute_command(
                f"mkdir -p /workspace/.setup_agent/tool_traces && echo '{escaped_output}' > {output_file}"
            )

    def get_execution_summary(self) -> Dict[str, Any]:
        """Get a summary of the execution."""
        thinking_actions = len([s for s in self.steps if s.model_used and "o1" in s.model_used])
        action_actions = len(
            [s for s in self.steps if s.model_used and "o1" not in (s.model_used or "")]
        )

        return {
            "total_steps": len(self.steps),
            "iterations": self.current_iteration,
            "thoughts": len([s for s in self.steps if s.step_type == StepType.THOUGHT]),
            "actions": len([s for s in self.steps if s.step_type == StepType.ACTION]),
            "observations": len([s for s in self.steps if s.step_type == StepType.OBSERVATION]),
            "thinking_model_calls": thinking_actions,
            "action_model_calls": action_actions,
            "successful_actions": len(
                [
                    s
                    for s in self.steps
                    if s.step_type == StepType.ACTION and s.tool_result and s.tool_result.success
                ]
            ),
            "failed_actions": len(
                [
                    s
                    for s in self.steps
                    if s.step_type == StepType.ACTION
                    and s.tool_result
                    and not s.tool_result.success
                ]
            ),
        }

    def _preserve_critical_info(self) -> str:
        """
        Preserve critical information that should not be forgotten due to context pollution.
        This acts as a 'short-term memory' protection mechanism.
        """
        critical_info = []
        
        # Always preserve repository URL if we have it
        if self.repository_url:
            critical_info.append(f"🔗 Repository URL: {self.repository_url}")
        
        # ENHANCED: Preserve dynamic project state information
        if self.successful_states.get('working_directory'):
            workdir = self.successful_states['working_directory']
            critical_info.append(f"📁 Working Directory: {workdir}")
            # Add explicit reminder for Maven/Gradle projects
            if workdir != "/workspace" and self.successful_states.get('project_type') in ['maven', 'gradle']:
                critical_info.append(f"⚠️ IMPORTANT: All Maven/Gradle commands must run in: {workdir}")
        
        # Preserve project structure awareness
        if self.successful_states.get('project_name'):
            critical_info.append(f"📦 Project Name: {self.successful_states['project_name']}")
        
        # Preserve build system information with more details
        build_system = self.successful_states.get('build_system')
        if build_system:
            status_indicator = " (working)" if self.successful_states.get('maven_success') else ""
            critical_info.append(f"🔧 Build System: {build_system}{status_indicator}")
        
        # Preserve critical project structure findings
        if self.successful_states.get('has_pom_xml'):
            critical_info.append("📄 Found: pom.xml (Maven project confirmed)")
        
        if self.successful_states.get('repository_cloned'):
            critical_info.append("✅ Repository: Successfully cloned")
        
        # Preserve successful tool states
        successful_tools = []
        for tool, success in [
            ('project_setup', 'project_setup' in self.successful_states.get('tools_used', [])),
            ('maven', self.successful_states.get('maven_success', False)),
            ('git', 'git' in self.successful_states.get('tools_used', [])),
        ]:
            if success:
                successful_tools.append(tool)
        
        if successful_tools:
            critical_info.append(f"✅ Working Tools: {', '.join(successful_tools)}")
        
        # CRITICAL: Preserve task plan to prevent context pollution from causing hallucinated task IDs
        try:
            # Use cached trunk context to avoid frequent file I/O
            trunk_context = self._get_cached_trunk_context()
            if trunk_context and trunk_context.todo_list:
                current_task = None
                next_task = None
                completed_count = 0
                
                for task in trunk_context.todo_list:
                    if task.status.value == "completed":
                        completed_count += 1
                    elif task.status.value == "in_progress":
                        current_task = task
                    elif task.status.value == "pending" and not next_task:
                        next_task = task
                
                plan_summary = [f"📋 TASK PLAN ({completed_count}/{len(trunk_context.todo_list)} completed):"]
                
                if current_task:
                    plan_summary.append(f"  🔄 CURRENT: {current_task.id} - {current_task.description}")
                
                if next_task:
                    plan_summary.append(f"  ⏭️ NEXT: {next_task.id} - {next_task.description}")
                
                # Show available task IDs to prevent hallucinations
                all_task_ids = [task.id for task in trunk_context.todo_list]
                plan_summary.append(f"  📝 VALID IDs: {', '.join(all_task_ids)}")
                
                # CRITICAL: Add previous task's key results as context for next task
                previous_task_results = []
                for task in trunk_context.todo_list:
                    if task.status.value == "completed" and task.key_results:
                        previous_task_results.append(f"    - {task.id}: {task.key_results}")
                
                if previous_task_results:
                    plan_summary.append("  🔑 PREVIOUS_TASK_RESULTS:")
                    plan_summary.extend(previous_task_results)
                
                # CRITICAL: Add clear workflow guidance to prevent mental model confusion
                plan_summary.append('  💡 WORKFLOW: manage_context(action="start_task", task_id="...") → [work on task] → manage_context(action="complete_with_results", summary="...", key_results="...")')
                plan_summary.append('  ⚠️ USE manage_context(action="complete_with_results", summary="...", key_results="...") - NOT a separate tool!')
                plan_summary.append("  ⚠️ NO 'branch_start' or 'branch_end' - context switching is automatic!")
                
                critical_info.extend(plan_summary)
                
        except Exception as e:
            # Don't let context loading errors break the memory protection
            critical_info.append("⚠️ Task plan unavailable - use manage_context(action='get_info')")
        
        if critical_info:
            return "\n🧠 CRITICAL MEMORY (preserved to prevent context pollution losses):\n" + "\n".join(critical_info) + "\n"
        return ""

    def _inject_memory_protection(self, prompt: str) -> str:
        """
        Inject critical information preservation into prompts to combat context pollution.
        """
        critical_memory = self._preserve_critical_info()
        if critical_memory:
            # Insert critical memory after the initial system prompt but before the current situation
            insertion_point = prompt.find("Current situation:")
            if insertion_point != -1:
                return prompt[:insertion_point] + critical_memory + "\n" + prompt[insertion_point:]
            else:
                # Fallback: add at the beginning
                return critical_memory + "\n" + prompt
        return prompt

    @deprecated("This is no longer used")
    def _check_task_completion_opportunity(self, observation: str):
        """
        Check if the observation indicates a task completion opportunity.
        Reminds Agent to use complete_with_results to avoid state/action separation.
        """
        if not self.context_manager.current_task_id:
            return  # Not in a task context
            
        # Define completion signals for different types of work
        completion_signals = {
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
                'project type:'
            ],
            'build_success': [
                'BUILD SUCCESS',
                'Tests run:',
                'compilation successful',
                'all tests passed'
            ],
            'environment_setup': [
                'environment configured',
                'dependencies installed',
                'setup completed'
            ]
        }
        
        observation_lower = observation.lower()
        detected_signals = []
        
        for signal_type, patterns in completion_signals.items():
            for pattern in patterns:
                if pattern.lower() in observation_lower:
                    detected_signals.append(signal_type)
                    break
        
        if detected_signals:
            # Add a system guidance to remind about state updates
            guidance_content = (
                f"🚨 TASK COMPLETION DETECTED: {', '.join(detected_signals)}\n"
                f"CRITICAL REMINDER: If this completes your current task ({self.context_manager.current_task_id}), "
                f"you MUST immediately call:\n"
                f"manage_context(action='complete_with_results', summary='...', key_results='...')\n"
                f"Do NOT continue to other tasks without updating the official task status!\n"
                f"This prevents 'ghost states' where work is done but not recorded."
            )
            
            guidance_step = ReActStep(
                step_type=StepType.SYSTEM_GUIDANCE,
                content=guidance_content,
                timestamp=self._get_timestamp()
            )
            self.steps.append(guidance_step)
            
            logger.info(f"🚨 Task completion opportunity detected: {detected_signals}")
            self.agent_logger.info(f"🚨 TASK COMPLETION GUIDANCE: {guidance_content}")

    def _get_cached_trunk_context(self):
        """
        Get trunk context with intelligent caching to avoid frequent file I/O.
        Only reloads when necessary (every 5 steps or after context changes).
        """
        current_step = len(self.steps)
        
        # Cache for 5 steps or if cache is empty
        if (self._cached_trunk_context is None or 
            self._trunk_context_cache_timestamp is None or 
            current_step - self._trunk_context_cache_timestamp >= 5):
            
            try:
                self._cached_trunk_context = self.context_manager.load_trunk_context()
                self._trunk_context_cache_timestamp = current_step
                logger.debug(f"Refreshed trunk context cache at step {current_step}")
            except Exception as e:
                logger.warning(f"Failed to refresh trunk context cache: {e}")
                # Keep using old cache if refresh fails
        
        return self._cached_trunk_context

    def _invalidate_trunk_cache(self):
        """Invalidate trunk context cache when we know it has changed."""
        self._cached_trunk_context = None
        self._trunk_context_cache_timestamp = None
        logger.debug("Trunk context cache invalidated")

    def _add_system_guidance(self, guidance_message: str, priority: int = 5):
        """
        Add system guidance with priority handling.
        Higher priority messages are more prominent.
        """
        # Add visual emphasis based on priority
        if priority >= 9:
            prefix = "🚨 CRITICAL GUIDANCE"
        elif priority >= 7:
            prefix = "⚠️ IMPORTANT GUIDANCE"
        else:
            prefix = "💡 SYSTEM GUIDANCE"
            
        full_message = f"{prefix} (Priority: {priority}):\n{guidance_message}"
        
        guidance_step = ReActStep(
            step_type=StepType.SYSTEM_GUIDANCE,
            content=full_message,
            timestamp=self._get_timestamp(),
        )
        self.steps.append(guidance_step)
        
        self.agent_logger.info(f"{prefix}: {guidance_message[:100]}...")
        logger.info(f"{prefix} added with priority {priority}")

    def test_state_evaluator_integration(self):
        """Test method to verify state evaluator is working correctly."""
        logger.info("Testing state evaluator integration...")
        
        # Simulate some steps
        test_steps = [
            ReActStep(step_type=StepType.THOUGHT, content="Test thought", timestamp=self._get_timestamp()),
            ReActStep(step_type=StepType.ACTION, content="Test action", timestamp=self._get_timestamp()),
            ReActStep(step_type=StepType.OBSERVATION, content="BUILD SUCCESS", timestamp=self._get_timestamp())
        ]
        
        # Test evaluation
        analysis = self.state_evaluator.evaluate(
            steps=test_steps,
            current_iteration=1,
            recent_tool_executions=[],
            steps_since_context_switch=5
        )
        
        logger.info(f"State analysis: {analysis.status}, needs_guidance: {analysis.needs_guidance}")
        return analysis

    def _build_thinking_model_prompt(self, base_prompt: str) -> str:
        """
        Build specialized prompt for thinking model.
        Thinking model should ONLY reason and analyze, never call tools.
        """
        thinking_instructions = """
🧠 THINKING MODEL INSTRUCTIONS:
You are in THINKING MODE. Your role is to analyze, reason, and plan - NOT to take actions.

CRITICAL RULES FOR REACT ARCHITECTURE:
1. ✅ OUTPUT ONLY THOUGHTS - Never attempt tool calls or function invocations
2. ✅ Your job is ANALYSIS and PLANNING - not execution
3. ✅ End your response with what ACTION should be taken next
4. ✅ The ACTION MODEL will handle tool execution based on your analysis
5. ✅ Do NOT format ACTION/PARAMETERS - that's the action model's job

YOUR OUTPUT FORMAT:
Provide pure reasoning and analysis. Always end with a clear recommendation:
"Based on this analysis, the next action should be: [describe what tool should be used and why]"

NEVER INCLUDE:
- Function calls or tool invocations  
- ACTION: statements
- PARAMETERS: blocks
- JSON formatting
- Any executable commands

REACT FLOW: THINKING → [hand off to ACTION MODEL] → OBSERVATION → [back to THINKING]

EXAMPLE OF CORRECT THINKING OUTPUT:
"I need to analyze the current project state. Looking at the context, I can see that task_1 requires cloning the repository. The repository URL is already provided: https://github.com/apache/commons-cli.git. 

The agent needs to start by cloning the repository to establish the workspace, then immediately read the project documentation to understand the proper setup process.

The logical sequence should be:
1. Clone the repository to /workspace
2. Read README.md to understand setup requirements  
3. Look for build instructions and dependencies
4. Follow the project's own setup instructions
5. Execute build and test commands as recommended in documentation

Based on this analysis, the next action should be: Use the project_setup tool with action='clone' to download the repository, since this is the foundational step that enables all subsequent work."

Remember: You are the THINKING brain, not the ACTING hands. Analyze and recommend, don't execute.

---

CURRENT SITUATION TO ANALYZE:
"""
        
        return thinking_instructions + base_prompt
        
    def _build_action_model_prompt(self, base_prompt: str) -> str:
        """
        Build specialized prompt for action model.
        Action model should execute tools based on reasoning.
        """
        action_instructions = """
🔧 ACTION MODEL INSTRUCTIONS:
You are in ACTION MODE. Your role is to execute specific actions based on thinking model analysis.

CRITICAL RULES FOR REACT ARCHITECTURE:
1. ✅ EXECUTE the action recommended by the thinking model
2. ✅ Use proper tool calling format (ACTION: tool_name, PARAMETERS: {...})
3. ✅ Don't re-analyze - the thinking model already did that
4. ✅ Focus on precise tool execution, not deep reasoning
5. ✅ Your job is DOING, not thinking

REACT FLOW: [THINKING complete] → ACTION (you) → OBSERVATION → [back to THINKING]

RESPONSE FORMAT (when function calling is supported):
Use function calling directly to execute the recommended tool. Minimal reasoning needed.

RESPONSE FORMAT (when function calling not supported):
ACTION: [tool_name]
PARAMETERS: [JSON object with required parameters]

CRITICAL: If the thinking model recommended a specific tool and action, execute it precisely.

AVAILABLE TOOLS AND THEIR PURPOSE:
- project_setup: Clone repositories and detect project types
- bash: Execute shell commands for system operations
- maven: Run Maven build commands
- file_io: Read and write files
- manage_context: Manage task workflow and completion

📖 PROJECT SETUP ACTION PRIORITIES:
1. After cloning, IMMEDIATELY read project documentation:
   • file_io(action="read", file_path="README.md") 
   • Look for setup instructions, build commands, dependencies
   • Check for testing procedures and environment requirements
2. Follow the project's own instructions rather than making assumptions
3. Use the exact commands recommended in the documentation

Remember: You are the ACTING hands, not the thinking brain. Execute the planned actions efficiently.

---

EXECUTE ACTIONS FOR:
"""
        
        return action_instructions + base_prompt

    def _attempt_error_recovery(self, tool_name: str, params: Dict[str, Any], failed_result: ToolResult) -> Dict[str, Any]:
        """
        Attempt to recover from tool execution failures using intelligent strategies.
        This is critical for robustness when individual tools fail.
        """
        try:
            recovery_info = {
                "attempted": False,
                "success": False,
                "message": "",
                "result": None
            }
            
            error_msg = failed_result.error or "Unknown error"
            error_code = getattr(failed_result, 'error_code', None)
            
            logger.info(f"🔧 Attempting error recovery for {tool_name}: {error_msg[:100]}")
            
            # Context management tool recovery
            if tool_name == "manage_context":
                recovery_info = self._recover_context_management_error(params, failed_result)
            
            # Maven tool recovery
            elif tool_name == "maven":
                recovery_info = self._recover_maven_error(params, failed_result)
            
            # Project setup tool recovery
            elif tool_name == "project_setup":
                recovery_info = self._recover_project_setup_error(params, failed_result)
            
            # Bash tool recovery
            elif tool_name == "bash":
                recovery_info = self._recover_bash_error(params, failed_result)
            
            # File I/O tool recovery
            elif tool_name == "file_io":
                recovery_info = self._recover_file_io_error(params, failed_result)
            
            else:
                # Generic recovery strategies
                recovery_info = self._recover_generic_error(tool_name, params, failed_result)
            
            if recovery_info["attempted"]:
                logger.info(f"Recovery attempt for {tool_name}: {recovery_info['message']}")
            
            return recovery_info
            
        except Exception as e:
            logger.error(f"Error recovery itself failed for {tool_name}: {e}")
            return {
                "attempted": False,
                "success": False,
                "message": f"Recovery mechanism failed: {str(e)}",
                "result": None
            }

    def _recover_context_management_error(self, params: Dict[str, Any], failed_result: ToolResult) -> Dict[str, Any]:
        """Recover from context management tool failures."""
        action = params.get("action", "")
        error_code = getattr(failed_result, 'error_code', None)
        
        # Handle "No active task to complete" error
        if error_code == "NO_ACTIVE_TASK" and action in ["complete_task", "complete_with_results"]:
            # Try to recover by checking trunk context state
            try:
                trunk_context = self.context_manager.load_trunk_context()
                if trunk_context:
                    # Look for in-progress tasks
                    in_progress_tasks = [t for t in trunk_context.todo_list if t.status.value == "in_progress"]
                    
                    if len(in_progress_tasks) == 1:
                        # Found one in-progress task - set it as current and retry
                        recovered_task = in_progress_tasks[0]
                        self.context_manager.current_task_id = recovered_task.id
                        
                        # Retry the operation
                        tool = self.tools["manage_context"]
                        result = tool.safe_execute(**params)
                        
                        return {
                            "attempted": True,
                            "success": result.success,
                            "message": f"Recovered by setting current task to {recovered_task.id}",
                            "result": result
                        }
                    elif len(in_progress_tasks) > 1:
                        # Multiple in-progress tasks - choose most recent
                        recovered_task = max(in_progress_tasks, key=lambda t: t.id)
                        self.context_manager.current_task_id = recovered_task.id
                        
                        tool = self.tools["manage_context"]
                        result = tool.safe_execute(**params)
                        
                        return {
                            "attempted": True,
                            "success": result.success,
                            "message": f"Recovered by choosing most recent task {recovered_task.id} from multiple in-progress",
                            "result": result
                        }
                        
            except Exception as e:
                logger.warning(f"Context recovery failed: {e}")
        
        # Handle invalid task ID errors
        elif error_code == "INVALID_TASK_ID" and action == "start_task":
            # Try to find the next valid task
            try:
                trunk_context = self.context_manager.load_trunk_context()
                if trunk_context:
                    next_task = trunk_context.get_next_pending_task()
                    if next_task:
                        # Update params with valid task ID and retry
                        recovery_params = params.copy()
                        recovery_params["task_id"] = next_task.id
                        
                        tool = self.tools["manage_context"]
                        result = tool.safe_execute(**recovery_params)
                        
                        return {
                            "attempted": True,
                            "success": result.success,
                            "message": f"Recovered by using next valid task ID: {next_task.id}",
                            "result": result
                        }
            except Exception as e:
                logger.warning(f"Task ID recovery failed: {e}")
        
        return {"attempted": False, "success": False, "message": "No recovery strategy applicable"}

    def _recover_maven_error(self, params: Dict[str, Any], failed_result: ToolResult) -> Dict[str, Any]:
        """Recover from Maven tool failures."""
        error_msg = failed_result.error or ""
        error_code = getattr(failed_result, 'error_code', None)
        
        # Check for Java version mismatch (highest priority)
        if error_code == "JAVA_VERSION_MISMATCH":
            # Extract required Java version from metadata
            metadata = getattr(failed_result, 'metadata', {})
            analysis = metadata.get('analysis', {})
            java_error = analysis.get('java_version_error', {})
            
            if java_error and java_error.get('required'):
                required_version = java_error['required']
                current_version = java_error.get('current', 'unknown')
                
                logger.info(f"🔧 Attempting Java version recovery: Installing Java {required_version} (current: {current_version})")
                
                # First, verify the current Java version
                if "system" in self.tools:
                    verify_result = self.tools["system"].safe_execute(
                        action="verify_java",
                        java_version=required_version
                    )
                    
                    # If verification shows we already have the right version, just retry Maven
                    if verify_result.success:
                        logger.info(f"Java {required_version} is already installed, retrying Maven command")
                        tool = self.tools["maven"]
                        result = tool.safe_execute(**params)
                        return {
                            "attempted": True,
                            "success": result.success,
                            "message": f"Java {required_version} was already installed, retried Maven command",
                            "result": result
                        }
                    
                    # Install the required Java version
                    install_result = self.tools["system"].safe_execute(
                        action="install_java",
                        java_version=required_version
                    )
                    
                    if install_result.success:
                        logger.info(f"✅ Successfully installed Java {required_version}, retrying Maven command")
                        
                        # Retry the original Maven command
                        tool = self.tools["maven"]
                        result = tool.safe_execute(**params)
                        
                        return {
                            "attempted": True,
                            "success": result.success,
                            "message": f"Recovered by installing Java {required_version} and retrying",
                            "result": result
                        }
                    else:
                        logger.warning(f"Failed to install Java {required_version}: {install_result.error}")
                        return {
                            "attempted": True,
                            "success": False,
                            "message": f"Attempted to install Java {required_version} but failed",
                            "result": install_result
                        }
                else:
                    logger.warning("System tool not available for Java installation")
        
        # Try to fix working directory issues
        if "not found" in error_msg.lower() or "no such file" in error_msg.lower():
            if self.successful_states.get('working_directory'):
                recovery_params = params.copy()
                recovery_params["working_directory"] = self.successful_states['working_directory']
                
                tool = self.tools["maven"]
                result = tool.safe_execute(**recovery_params)
                
                return {
                    "attempted": True,
                    "success": result.success,
                    "message": f"Recovered by using known working directory: {self.successful_states['working_directory']}",
                    "result": result
                }
        
        # Try to simplify Maven command for initial failures
        command = params.get("command", "")
        if "test" in command and "compilation" in error_msg.lower():
            # If test failed due to compilation, try just compile first
            recovery_params = params.copy()
            recovery_params["command"] = "compile"
            
            tool = self.tools["maven"]
            result = tool.safe_execute(**recovery_params)
            
            return {
                "attempted": True,
                "success": result.success,
                "message": "Recovered by trying compile before test",
                "result": result
            }
        
        return {"attempted": False, "success": False, "message": "No Maven recovery strategy applicable"}

    def _recover_project_setup_error(self, params: Dict[str, Any], failed_result: ToolResult) -> Dict[str, Any]:
        """Recover from project setup tool failures."""
        action = params.get("action", "")
        
        # Auto-inject repository URL if missing
        if action == "clone" and not params.get("repository_url") and self.repository_url:
            recovery_params = params.copy()
            recovery_params["repository_url"] = self.repository_url
            
            tool = self.tools["project_setup"]
            result = tool.safe_execute(**recovery_params)
            
            return {
                "attempted": True,
                "success": result.success,
                "message": f"Recovered by injecting repository URL: {self.repository_url}",
                "result": result
            }
        
        return {"attempted": False, "success": False, "message": "No project setup recovery strategy applicable"}

    def _recover_bash_error(self, params: Dict[str, Any], failed_result: ToolResult) -> Dict[str, Any]:
        """
        Recover from bash tool failures.
        
        ★ PRIORITY FIX: Add chain recovery for exit code 127 (OCI runtime exec failed).
        When workspace directory is missing, fix it and retry the same command.
        """
        error_msg = failed_result.error or ""
        
        # ★ CRITICAL RECOVERY: Handle exit code 127 / OCI runtime exec failed
        if hasattr(failed_result, 'metadata') and failed_result.metadata:
            exit_code = failed_result.metadata.get('exit_code', 0)
            
            if exit_code == 127 or "OCI runtime exec failed" in error_msg or "no such file or directory" in error_msg:
                logger.info("🔧 RECOVERY: Detected exit code 127 / workspace directory issue")
                
                # Try to recreate workspace and retry the exact same command
                recovery_steps = [
                    ("mkdir -p /workspace", "Create workspace directory"),
                    ("chmod 755 /workspace", "Set workspace permissions"),
                    ("touch /workspace/.sag_workspace_marker", "Create workspace marker")
                ]
                
                # Execute recovery steps
                workspace_fixed = True
                for recovery_cmd, description in recovery_steps:
                    logger.info(f"🔧 RECOVERY STEP: {description}")
                    # Use the orchestrator directly to avoid recursion
                    recovery_result = self.docker_orchestrator.execute_command(recovery_cmd, workdir=None)
                    
                    if not recovery_result["success"]:
                        logger.warning(f"⚠️ Recovery step failed: {description}")
                        workspace_fixed = False
                        break
                    else:
                        logger.info(f"✅ Recovery step successful: {description}")
                
                if workspace_fixed:
                    # Retry the original command with fixed workspace
                    logger.info("🔧 RECOVERY: Retrying original command after workspace fix")
                    recovery_params = params.copy()
                    recovery_params["working_directory"] = "/workspace"  # Force workspace use
                    
                    tool = self.tools["bash"]
                    result = tool.safe_execute(**recovery_params)
                    
                    if result.success:
                        logger.info("✅ RECOVERY SUCCESS: Command succeeded after workspace fix")
                        return {
                            "attempted": True,
                            "success": True,
                            "message": "Recovered by recreating workspace directory and retrying command",
                            "result": result
                        }
                    else:
                        logger.warning("⚠️ RECOVERY PARTIAL: Workspace fixed but command still failed")
                        return {
                            "attempted": True,
                            "success": False,
                            "message": "Workspace recreated but command still failed - may be a different issue",
                            "result": result
                        }
                else:
                    logger.error("❌ RECOVERY FAILED: Could not recreate workspace directory")
                    return {
                        "attempted": True,
                        "success": False,
                        "message": "Failed to recreate workspace directory",
                        "result": None
                    }
        
        # Fallback: Try to fix working directory issues using successful states
        if self.successful_states.get('working_directory'):
            logger.info(f"🔧 RECOVERY: Trying known working directory: {self.successful_states['working_directory']}")
            recovery_params = params.copy()
            recovery_params["working_directory"] = self.successful_states['working_directory']
            
            tool = self.tools["bash"]
            result = tool.safe_execute(**recovery_params)
            
            return {
                "attempted": True,
                "success": result.success,
                "message": f"Recovered by using known working directory: {self.successful_states['working_directory']}",
                "result": result
            }
        
        return {"attempted": False, "success": False, "message": "No bash recovery strategy applicable"}

    def _recover_file_io_error(self, params: Dict[str, Any], failed_result: ToolResult) -> Dict[str, Any]:
        """Recover from file I/O tool failures."""
        action = params.get("action", "")
        path = params.get("path", "")
        
        # Try to fix path issues with working directory context
        if action == "read" and "not found" in (failed_result.error or "").lower():
            if self.successful_states.get('working_directory') and not path.startswith('/'):
                # Try with working directory prefix
                recovery_params = params.copy()
                recovery_params["path"] = f"{self.successful_states['working_directory']}/{path}"
                
                tool = self.tools["file_io"]
                result = tool.safe_execute(**recovery_params)
                
                return {
                    "attempted": True,
                    "success": result.success,
                    "message": f"Recovered by adjusting path with working directory",
                    "result": result
                }
        
        return {"attempted": False, "success": False, "message": "No file I/O recovery strategy applicable"}

    def _recover_generic_error(self, tool_name: str, params: Dict[str, Any], failed_result: ToolResult) -> Dict[str, Any]:
        """Generic recovery strategies for any tool."""
        # For now, just return no recovery
        # Can be extended with more generic strategies
        return {"attempted": False, "success": False, "message": "No generic recovery strategy available"}

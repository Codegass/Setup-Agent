"""ReAct Engine for Setup-Agent (SAG)."""

import json
import re
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import litellm
from loguru import logger
from pydantic import BaseModel

from config import create_agent_logger, create_verbose_logger, get_config
from tools import BaseTool, ToolResult

from .context_manager import BranchContext, ContextManager, TrunkContext


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

        logger.info("ReAct Engine initialized with dual model support")
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

                # Check for completion
                if self._is_task_complete():
                    self.agent_logger.info("Task completed successfully")
                    return True
                
                # Check if we should strongly suggest completion (rule-based)
                completion_suggestion = self._check_completion_suggestion()
                if completion_suggestion:
                    self._add_strong_completion_guidance(completion_suggestion)

                # Check for context switching guidance
                self._check_context_switching_guidance()
                
                # Check if model needs explicit action guidance
                if self._needs_action_guidance():
                    self._add_action_guidance()

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
        """Determine if we should use the thinking model for this step."""
        # Check if thinking model was explicitly requested due to repetitive execution
        if self._force_thinking_next:
            self._force_thinking_next = False  # Reset the flag
            logger.info("Using thinking model due to repetitive execution detection")
            return True
        
        # Use thinking model ONLY for pure reasoning, not for tool execution
        # The thinking model should only be used for complex analysis and planning
        
        # For now, let's primarily use the action model which has function calling support
        # Use thinking model only for the very first step for initial analysis
        if self.current_iteration == 1:
            return True

        # Check if we've had thinking steps recently
        recent_steps = self.steps[-5:] if len(self.steps) >= 5 else self.steps
        thinking_model_name = self.config.get_litellm_model_name("thinking")
        thinking_steps = [s for s in recent_steps if s.model_used and thinking_model_name in s.model_used]

        # Use thinking model when we encounter many errors (need deep analysis)
        recent_errors = [
            s
            for s in recent_steps
            if s.step_type == StepType.ACTION and s.tool_result and not s.tool_result.success
        ]

        if len(recent_errors) >= 3:  # Increased threshold
            return True

        # Otherwise, use action model which has function calling support
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

                # Log detailed request in verbose mode
                if self.config.verbose:
                    self._log_llm_request(
                        model, prompt, temperature, max_tokens, use_thinking_model
                    )

                # Get thinking configuration based on provider
                thinking_config = self.config.get_thinking_config()

                # Special handling for different thinking models
                if thinking_config:
                    # For models with thinking capabilities (o1, claude)
                    response = litellm.completion(
                        model=model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=temperature,
                        max_tokens=max_tokens,
                        **thinking_config,
                    )
                else:
                    # For regular models
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

                # Special handling for O-series models (only support temperature=1)
                if (
                    "o4" in self.config.action_model.lower()
                    or "o1" in self.config.action_model.lower()
                ):
                    temperature = 1.0

                # Log detailed request in verbose mode
                if self.config.verbose:
                    self._log_llm_request(
                        model, prompt, temperature, max_tokens, use_thinking_model
                    )

                # Build parameters for the request
                request_params = {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
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
- You work with TWO types of contexts: TRUNK (main) and BRANCH (sub-task)
- TRUNK context: Contains the overall goal and TODO list
- BRANCH context: For focused work on specific tasks
- ALWAYS use manage_context tool to switch between contexts appropriately
- When starting a new task from TODO list, create a branch context
- When completing a task, return to trunk context with a summary

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
- manage_context: Manage context switching (NOT context)
  • Valid actions: get_info, create_branch, switch_to_trunk
  • For create_branch: REQUIRES task_id parameter (e.g., task_id="build_project")
  • For switch_to_trunk: Optional summary parameter
  • Example: manage_context(action="get_info")
- maven: Execute Maven commands (NOT mvn)
- project_setup: Clone repositories and setup projects (NOT git_clone or clone)
- system: Install system packages and dependencies

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
1. Start by using the tools - don't ask questions, take action!"""
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
1. ALWAYS start with: manage_context(action="get_info")"""

        # Add repository URL instruction if available
        if self.repository_url:
            prompt += f"""
2. ALWAYS clone repository with: project_setup(action="clone", repository_url="{self.repository_url}")"""
        else:
            prompt += """
2. ALWAYS clone repository with: project_setup(action="clone", repository_url="<REPOSITORY_URL>")"""

        prompt += """
3. ALWAYS detect project type: project_setup(action="detect_project_type")
4. For Maven projects: maven(command="compile") or maven(command="test")
5. For shell commands: bash(command="ls -la")
6. For reading files: file_io(action="read", file_path="/path/to/file")

NEVER use: git_clone, shell, python, clone, read_file, write_file, mvn, etc.

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
            # Try to extract any content as a thought
            content = response.strip()
            if content:
                logger.info(f"Parsing failed, treating entire response as thought")
                logger.info(f"Full response content: {content}")
                steps.append(
                    ReActStep(
                        step_type=StepType.THOUGHT,
                        content=content,
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

                # Check for repetitive tool execution
                tool_signature = f"{step.tool_name}:{str(sorted(validated_params.items()))}"
                if self._is_repetitive_execution(tool_signature):
                    logger.warning(f"Detected repetitive execution of {step.tool_name}, adding guidance and triggering thinking model")
                    
                    # Force a switch to thinking model in next iteration
                    self._force_thinking_next = True
                    
                    # Provide detailed guidance
                    recent_executions = [e for e in self.recent_tool_executions 
                                       if e["signature"].startswith(step.tool_name + ':')]
                    failure_count = sum(1 for e in recent_executions if not e["success"])
                    
                    guidance_msg = (f"Tool {step.tool_name} has been executed repeatedly with {failure_count} failures. "
                                  f"This suggests the current approach is not working. ")
                    
                    if step.tool_name == "maven":
                        guidance_msg += ("Consider checking the project structure, examining build errors in detail, "
                                       "or switching to bash tool to investigate the issue manually.")
                    elif step.tool_name == "bash":
                        guidance_msg += ("Consider checking command syntax, working directory, or using file_io "
                                       "to examine files before executing commands.")
                    else:
                        guidance_msg += ("Consider examining the error messages, changing parameters, "
                                       "or using a different tool to achieve the same goal.")
                    
                    result = ToolResult(
                        success=False,
                        output="",
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
                    
                    # Update successful states for future reference
                    if result.success:
                        self._update_successful_states(step.tool_name, validated_params, result)

                step.tool_result = result

                # Log tool result in verbose mode
                if self.config.verbose:
                    self._log_tool_result_verbose(step.tool_name, result)

                # Add observation step with improved formatting
                self._add_observation_step(self._format_tool_result(step.tool_name, result))

                # Log to branch context if we're in one
                if self.context_manager.current_task_id:
                    # Add action result to branch history using new context management system
                    try:
                        self.context_manager.add_to_branch_history(
                            self.context_manager.current_task_id,
                            {
                                "type": "action",
                                "tool_name": step.tool_name,
                                "success": result.success,
                                "output": result.output[:500] if result.output else ""  # Truncate long outputs
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
                        return {}
                return value if isinstance(value, dict) else {}
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
                'workdir': 'working_directory',
                'work_dir': 'working_directory',
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
                'description': 'summary',
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
                fixed_params[new_name] = fixed_params[old_name]
                del fixed_params[old_name]
                mappings_applied.append(f"{old_name} → {new_name}")
                logger.info(f"🔧 Renamed parameter '{old_name}' to '{new_name}' for {tool_name}")
        
        # Log all mappings applied for debugging
        if mappings_applied:
            logger.debug(f"Parameter mappings applied for {tool_name}: {', '.join(mappings_applied)}")
        
        return fixed_params

    def _update_successful_states(self, tool_name: str, params: Dict[str, Any], result: ToolResult):
        """Update successful states based on tool execution results."""
        try:
            if tool_name == "bash" and params.get("working_directory"):
                # Remember successful working directory
                self.successful_states['working_directory'] = params['working_directory']
                logger.debug(f"Updated successful working directory: {params['working_directory']}")
            
            elif tool_name == "maven" and params.get("working_directory"):
                # Remember successful Maven working directory
                if "BUILD SUCCESS" in (result.output or ""):
                    self.successful_states['working_directory'] = params['working_directory']
                    self.successful_states['maven_success'] = True
                    logger.info(f"Maven success recorded for directory: {params['working_directory']}")
            
            elif tool_name == "project_setup":
                # Remember cloned repositories and project type
                if params.get("repository_url"):
                    self.successful_states['cloned_repos'].add(params['repository_url'])
                    logger.debug(f"Recorded cloned repo: {params['repository_url']}")
                
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
                
            # If reading but no file path, default to current directory listing
            if fixed_params.get("action") == "read" and not fixed_params.get("path"):
                fixed_params["action"] = "list"
                fixed_params["path"] = self.successful_states['working_directory'] or "/workspace"
                
        elif tool_name == "manage_context":
            # Fix common action name errors
            action = fixed_params.get("action", "")
            if action == "switch":
                # Convert "switch" to "switch_to_trunk" as default
                fixed_params["action"] = "switch_to_trunk"
                logger.info(f"🔧 Converted action 'switch' to 'switch_to_trunk' for manage_context")
            elif action == "info":
                fixed_params["action"] = "get_info"
                logger.info(f"🔧 Converted action 'info' to 'get_info' for manage_context")
            elif action == "create":
                fixed_params["action"] = "create_branch"
                logger.info(f"🔧 Converted action 'create' to 'create_branch' for manage_context")
            
            # Ensure required parameters for create_branch
            if fixed_params.get("action") == "create_branch":
                if not fixed_params.get("task_id"):
                    # Generate a default task_id if missing
                    summary = fixed_params.get("summary", "default_task")
                    task_id = summary.replace(" ", "_").lower()[:20]
                    fixed_params["task_id"] = task_id
                    logger.info(f"🔧 Generated missing task_id: {task_id}")
        
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
            
            if result.suggestions:
                formatted += f"\n\nSuggestions:\n" + "\n".join(f"• {s}" for s in result.suggestions[:3])
                
            if result.error_code:
                formatted += f"\nError code: {result.error_code}"
            
            # Add full raw output if available and error message is unclear
            if result.raw_output and (not result.error or len(result.error.strip()) < 10):
                formatted += f"\n\nRaw output: {result.raw_output}"
                
        return formatted



    def _is_repetitive_execution(self, tool_signature: str) -> bool:
        """Check if this tool execution is repetitive."""
        # Count recent executions of the same tool with same parameters
        exact_match_count = sum(1 for exec_info in self.recent_tool_executions 
                               if exec_info["signature"] == tool_signature)
        
        # Extract tool name from signature
        tool_name = tool_signature.split(':')[0]
        
        # Count recent executions of the same tool (regardless of parameters)
        tool_executions = [exec_info for exec_info in self.recent_tool_executions 
                          if exec_info["signature"].startswith(tool_name + ':')]
        
        # Check for patterns that indicate repetitive execution
        recent_tool_count = len(tool_executions)
        recent_failures = sum(1 for exec_info in tool_executions if not exec_info["success"])
        
        # More lenient thresholds to avoid blocking legitimate operations
        # Block if: 
        # 1. Exact same call attempted 3+ times, OR
        # 2. Same tool failed 5+ times recently, OR  
        # 3. Same tool called 8+ times in recent executions
        return (exact_match_count >= 3 or 
                recent_failures >= 5 or 
                recent_tool_count >= 8)

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
        """Add an observation step."""
        obs_step = ReActStep(
            step_type=StepType.OBSERVATION, content=observation, timestamp=self._get_timestamp()
        )
        self.steps.append(obs_step)

        # Log full observation content without truncation
        self.agent_logger.info(f"👁️ OBSERVATION: {observation}")
        logger.info(f"👁️ OBSERVATION: {observation}")

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
        
        # Look at recent steps for completion indicators
        recent_steps = self.steps[-5:] if len(self.steps) >= 5 else self.steps

        for step in recent_steps:
            if step.step_type == StepType.THOUGHT:
                content_lower = step.content.lower()
                if any(
                    phrase in content_lower
                    for phrase in [
                        "task completed",
                        "setup complete",
                        "finished",
                        "done",
                        "successfully completed",
                        "all tasks completed",
                        "build and test complete",
                        "maven build successful",
                        "tests passed successfully",
                    ]
                ):
                    return True

            elif step.step_type == StepType.ACTION and step.tool_name == "manage_context":
                # If we're switching to trunk with a completion summary
                if (
                    step.tool_params
                    and step.tool_params.get("action") == "switch_to_trunk"
                    and step.tool_params.get("summary")
                ):
                    summary = step.tool_params.get("summary", "").lower()
                    if "completed" in summary or "success" in summary or "finished" in summary:
                        return True

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
        if self.current_iteration >= 15 and not self._has_report_been_generated():
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
    
    def _add_strong_completion_guidance(self, reason: str):
        """Add strong guidance to push agent toward completion."""
        guidance = (f"🚨 URGENT COMPLETION NOTICE: {reason}. "
                   f"You MUST now call the report tool to generate a completion summary. "
                   f"Example: report(action='generate_completion_report', "
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

USE ONE OF THESE ACTIONS NOW:
1. Clone the repository:
   Call project_setup with action="clone" and repository_url="{self.repository_url}"

2. Or check context first:
   Call manage_context with action="get_info"

DO NOT ask for the repository URL - it's already provided above!
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
        return prompt

    def _get_timestamp(self) -> str:
        """Get current timestamp string."""
        from datetime import datetime

        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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

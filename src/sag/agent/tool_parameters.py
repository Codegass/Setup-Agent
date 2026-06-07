"""Internal parameter normalization for orchestrated tool execution."""

from __future__ import annotations

import re
import shlex
from typing import Any, Dict, Optional

from loguru import logger as default_logger

from sag.agent.tool_orchestration import ParameterFix, ParameterFixSource
from sag.tools.base import BaseTool


class ToolParameterNormalizer:
    """Normalize model-supplied tool parameters against tool schemas and runtime state."""

    def __init__(
        self,
        *,
        tools: Dict[str, BaseTool],
        successful_states: Dict[str, Any],
        repository_url: Optional[str],
        logger: Any = None,
    ) -> None:
        self.tools = tools
        self.successful_states = successful_states
        self.repository_url = repository_url
        self.logger = logger or default_logger

    def _add_parameter_fix(
        self,
        fixes: list[ParameterFix],
        *,
        field: str,
        before: Any,
        after: Any,
        reason: str,
        source: ParameterFixSource,
    ) -> None:
        if before != after:
            fixes.append(
                ParameterFix(
                    field=field,
                    before=before,
                    after=after,
                    reason=reason,
                    source=source,
                )
            )

    def _append_maven_fail_at_end_if_needed(self, command: str) -> tuple[str, bool]:
        """Append Maven fail-at-end only when a shell segment invokes Maven directly."""
        if not command:
            return command, False

        # Piped or redirected commands are often inspection commands containing "mvn".
        # Leave those untouched rather than rewriting shell syntax we do not fully parse.
        if any(operator in command for operator in ("|", ">", "<")):
            return command, False

        parts = re.split(r"(\s*(?:&&|;)\s*)", command)
        changed = False
        rewritten_parts = []

        for part in parts:
            if re.fullmatch(r"\s*(?:&&|;)\s*", part):
                rewritten_parts.append(part)
                continue

            tokens = self._split_shell_segment(part)
            if not self._should_append_maven_fail_at_end(tokens):
                rewritten_parts.append(part)
                continue

            if "--fail-at-end" in tokens or "-fae" in tokens:
                rewritten_parts.append(part)
                continue

            rewritten_parts.append(f"{part.rstrip()} --fail-at-end")
            changed = True

        return "".join(rewritten_parts), changed

    def _split_shell_segment(self, segment: str) -> list[str]:
        try:
            return shlex.split(segment)
        except ValueError:
            return []

    def _should_append_maven_fail_at_end(self, tokens: list[str]) -> bool:
        command_index = self._maven_command_index(tokens)
        if command_index is None:
            return False

        args = tokens[command_index + 1 :]
        if any(arg in {"-v", "--version", "-version"} for arg in args):
            return False

        lifecycle_phases = {
            "validate",
            "initialize",
            "generate-sources",
            "process-sources",
            "generate-resources",
            "process-resources",
            "compile",
            "process-classes",
            "generate-test-sources",
            "process-test-sources",
            "generate-test-resources",
            "process-test-resources",
            "test-compile",
            "process-test-classes",
            "test",
            "prepare-package",
            "package",
            "pre-integration-test",
            "integration-test",
            "post-integration-test",
            "verify",
            "install",
            "deploy",
            "clean",
            "site",
        }
        return any(arg in lifecycle_phases for arg in args)

    def _maven_command_index(self, tokens: list[str]) -> Optional[int]:
        if not tokens:
            return None

        command_index = 0
        while command_index < len(tokens) and self._is_shell_assignment(tokens[command_index]):
            command_index += 1

        if command_index >= len(tokens):
            return None

        executable = tokens[command_index].rsplit("/", 1)[-1]
        if executable not in {"mvn", "mvnw"}:
            return None
        return command_index

    def _is_shell_assignment(self, token: str) -> bool:
        if "=" not in token:
            return False
        name = token.split("=", 1)[0]
        return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))

    def validate_and_fix(
        self,
        tool_name: str,
        params: Dict[str, Any],
        parameter_fixes: Optional[list[ParameterFix]] = None,
    ) -> Dict[str, Any]:
        """Validate and fix tool parameters with self-healing capability."""
        fixes = parameter_fixes if parameter_fixes is not None else []
        if tool_name not in self.tools:
            self.logger.error(f"Unknown tool: {tool_name}")
            return params

        tool = self.tools[tool_name]

        # Handle completely empty parameters
        if not params:
            params = {}

        # Get the tool's parameter schema
        if hasattr(tool, "get_parameter_schema"):
            schema = tool.get_parameter_schema()
        elif hasattr(tool, "_get_parameters_schema"):
            schema = tool._get_parameters_schema()
        else:
            # No schema available, apply basic fixes
            return self._apply_basic_parameter_fixes(tool_name, params, fixes)

        # Validate and fix parameters
        validated_params = self._fix_parameters_against_schema(params, schema, tool_name, fixes)

        # Apply additional tool-specific fixes
        validated_params = self._apply_tool_specific_fixes(tool_name, validated_params, fixes)

        # Check for unexpected parameters and provide warnings
        expected_params = set(schema.get("properties", {}).keys())
        actual_params = set(validated_params.keys())
        unexpected_params = actual_params - expected_params

        if unexpected_params:
            self.logger.warning(f"🚨 Unexpected parameters for {tool_name}: {unexpected_params}")
            self.logger.warning(f"Expected parameters: {expected_params}")

            # Only remove parameters that are clearly invalid, keep potentially useful ones
            params_to_remove = []
            for param in unexpected_params:
                param_value = validated_params[param]

                # Keep parameters that might be useful extensions
                if tool_name == "maven" and param in ["pom_file", "maven_home", "java_home"]:
                    self.logger.info(
                        f"🔧 Keeping potentially useful Maven parameter: {param}={param_value}"
                    )
                    continue
                elif tool_name == "bash" and param in ["env", "environment"]:
                    self.logger.info(
                        f"🔧 Keeping potentially useful bash parameter: {param}={param_value}"
                    )
                    continue
                elif tool_name == "system" and param in ["sudo", "force"]:
                    self.logger.info(
                        f"🔧 Keeping potentially useful system parameter: {param}={param_value}"
                    )
                    continue
                else:
                    # Remove clearly invalid parameters
                    params_to_remove.append(param)

            # DISABLED: Auto-removal of invalid parameters to enable proper error feedback
            # Let tools handle their own parameter validation and provide clear error messages
            # for param in params_to_remove:
            #     self.logger.warning(f"🔧 Removing invalid parameter: {param}={validated_params[param]}")
            #     del validated_params[param]

        # Log parameter fixes if any were made
        if validated_params != params:
            self.logger.info(f"🔧 Parameter self-healing applied for {tool_name}")
            self.logger.debug(f"Original params: {params}")
            self.logger.debug(f"Fixed params: {validated_params}")

        return validated_params

    def _fix_parameters_against_schema(
        self,
        params: Dict[str, Any],
        schema: Dict[str, Any],
        tool_name: str,
        parameter_fixes: Optional[list[ParameterFix]] = None,
    ) -> Dict[str, Any]:
        """Fix parameters against a schema with intelligent defaults."""
        fixes = parameter_fixes if parameter_fixes is not None else []
        fixed_params = params.copy()

        # Get schema properties
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        # Fix missing required parameters
        for param_name in required:
            if param_name not in fixed_params or fixed_params[param_name] is None:
                before = fixed_params.get(param_name)
                default_value = self._get_smart_default(
                    param_name, properties.get(param_name, {}), tool_name
                )
                if default_value is not None:
                    fixed_params[param_name] = default_value
                    self._add_parameter_fix(
                        fixes,
                        field=param_name,
                        before=before,
                        after=default_value,
                        reason=f"Added missing required parameter '{param_name}'",
                        source="default",
                    )
                    self.logger.info(
                        f"🔧 Added missing required parameter '{param_name}' with default: {default_value}"
                    )

        # Fix parameter types
        for param_name, param_value in fixed_params.items():
            if param_name in properties:
                prop_schema = properties[param_name]
                expected_type = prop_schema.get("type")

                # Try to convert to expected type
                if expected_type and param_value is not None:
                    converted_value = self._convert_parameter_type(
                        param_value, expected_type, param_name
                    )
                    if converted_value != param_value:
                        fixed_params[param_name] = converted_value
                        self._add_parameter_fix(
                            fixes,
                            field=param_name,
                            before=param_value,
                            after=converted_value,
                            reason=f"Converted parameter '{param_name}' to {expected_type}",
                            source="safety_fix",
                        )
                        self.logger.info(
                            f"🔧 Converted parameter '{param_name}' from {type(param_value).__name__} to {expected_type}"
                        )

        # Handle common parameter naming issues
        fixed_params = self._fix_parameter_names(fixed_params, properties, tool_name, fixes)

        return fixed_params

    def _get_smart_default(
        self, param_name: str, param_schema: Dict[str, Any], tool_name: str
    ) -> Any:
        """Get smart default values for common parameters."""
        param_type = param_schema.get("type", "string")

        # Check if there's a default in the schema
        if "default" in param_schema:
            return param_schema["default"]

        # Smart defaults based on parameter names and tool types
        smart_defaults = {
            # Command-related parameters
            "command": "help" if tool_name == "bash" else None,
            "cmd": "help",
            "timeout": 60,
            # File-related parameters
            "action": self._get_tool_specific_action_default(tool_name),
            "path": "/workspace",
            "file_path": "/workspace",
            "directory": "/workspace",
            "working_directory": "/workspace",
            # Web search parameters
            "query": "help" if tool_name == "web_search" else None,
            "max_results": 5,
            # System parameters
            "packages": [] if param_type == "array" else None,
            # Maven parameters
            "goals": None,
            "profiles": None,
            "properties": None,
            "raw_output": False,
            # Context management
            "context_type": "branch",
            "summary": "Task in progress",
            # Project setup parameters - DO NOT provide defaults for URLs
            # These should come from the user's actual repository URL
            "repository_url": None,
            "url": None,
            "repo_url": None,
            # Generic defaults by type
            "boolean": False,
            "integer": 0,
            "array": [],
            "object": {},
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
            "file_io": "read",
            "project_setup": "clone",
            "system": "install_missing",
            "manage_context": "get_info",
            "maven": "compile",
            "bash": None,
            "web_search": None,
        }
        return tool_action_defaults.get(tool_name, "list")

    def _convert_parameter_type(self, value: Any, expected_type: str, param_name: str) -> Any:
        """Convert parameter to expected type."""
        try:
            if expected_type == "string":
                # Handle list to string conversion properly
                if isinstance(value, list):
                    # If list has one element, return just that element
                    if len(value) == 1:
                        return str(value[0])
                    # If multiple elements, join with spaces (common for command-line args)
                    else:
                        return " ".join(str(v) for v in value)
                return str(value)
            elif expected_type == "integer":
                if isinstance(value, str):
                    # Try to extract number from string
                    import re

                    match = re.search(r"\d+", value)
                    if match:
                        return int(match.group())
                return int(value)
            elif expected_type == "boolean":
                if isinstance(value, str):
                    return value.lower() in ["true", "1", "yes", "on"]
                elif isinstance(value, list):
                    # Handle list to boolean conversion properly
                    if len(value) == 0:
                        return False  # Empty list = False
                    elif len(value) == 1:
                        # Single element - convert that element recursively
                        return self._convert_parameter_type(value[0], "boolean", param_name)
                    else:
                        # Multiple elements - true if any are true
                        return any(
                            self._convert_parameter_type(v, "boolean", param_name) for v in value
                        )
                return bool(value)
            elif expected_type == "array":
                if isinstance(value, str):
                    # Try to parse as JSON array or split by common delimiters
                    try:
                        import json

                        return json.loads(value)
                    except:
                        # Split by common delimiters
                        return [item.strip() for item in value.split(",")]
                elif not isinstance(value, list):
                    return [value]
                return value
            elif expected_type == "object":
                if isinstance(value, str):
                    try:
                        import json

                        return json.loads(value)
                    except:
                        # CRITICAL FIX: Don't lose the original string value!
                        # For manage_context entry parameter, wrap string in meaningful object
                        if param_name == "entry":
                            return {"content": value}  # Preserve the original string as content
                        elif "description" in param_name.lower() or "content" in param_name.lower():
                            return {"description": value}
                        else:
                            return {"value": value}  # Fallback: preserve in generic wrapper
                # Don't wrap lists of dicts unnecessarily
                if isinstance(value, list) and all(
                    isinstance(item, dict) for item in value if value
                ):
                    return value  # Return list of dicts as-is
                return value if isinstance(value, dict) else {"value": value}
        except Exception as e:
            self.logger.warning(
                f"Failed to convert parameter '{param_name}' to {expected_type}: {e}"
            )
            return value

        return value

    def _fix_parameter_names(
        self,
        params: Dict[str, Any],
        properties: Dict[str, Any],
        tool_name: str,
        parameter_fixes: Optional[list[ParameterFix]] = None,
    ) -> Dict[str, Any]:
        """Fix common parameter naming issues."""
        fixes = parameter_fixes if parameter_fixes is not None else []
        fixed_params = params.copy()

        # Common parameter name mappings (removed conflicting mappings)
        name_mappings = {
            # Action variations (file_io, context tools)
            "op": "action",
            "operation": "action",
            "method": "action",
            "type": "action",
            # Query variations (web_search tool)
            "search": "query",
            "q": "query",
            "term": "query",
            "search_term": "query",
            "keywords": "query",
            # URL variations (project_setup tool)
            "url": "repository_url",
            "repo_url": "repository_url",
            "git_url": "repository_url",
            "repository": "repository_url",
            "repo": "repository_url",
            "git_repo": "repository_url",
            # Target directory variations (project_setup tool)
            "destination": "target_directory",
            "dest": "target_directory",
            "target_dir": "target_directory",
            "output_dir": "target_directory",
            "clone_dir": "target_directory",
            # Maven/build specific (non-conflicting)
            "options": "properties",
            "opts": "properties",
            "maven_options": "properties",
            "build_options": "properties",
            # Context specific
            "context_type": "action",
            "name": "task_id",
            "parameters": "summary",
            "task_name": "task_id",
            "id": "task_id",
            # Content variations (file_io tool)
            "data": "content",
            "text": "content",
            "body": "content",
            "file_content": "content",
        }

        # Tool-specific mappings for better accuracy
        tool_specific_mappings = {
            "bash": {
                "cmd": "command",
                "script": "command",
                "exec": "command",
                "shell": "command",
                "run": "command",
                "execute": "command",
                "bash_command": "command",
                "shell_command": "command",
                "dir": "working_directory",
                "cwd": "working_directory",
                "working_dir": "working_directory",
                "workdir": "working_directory",  # Map old workdir to working_directory
                "work_dir": "working_directory",
                "directory": "working_directory",
                "path": "working_directory",  # Path should also map to working_directory for bash
            },
            "file_io": {
                "file": "path",
                "filename": "path",
                "filepath": "path",
                "file_path": "path",
                "operation": "action",
                "op": "action",
                "data": "content",
                "text": "content",
            },
            "project_setup": {
                "url": "repository_url",
                "repo": "repository_url",
                "destination": "target_directory",
                "dest": "target_directory",
                "output": "target_directory",
            },
            "maven": {
                # Don't map 'goals' - it's a separate parameter from 'command'
                "options": "properties",
                "dir": "working_directory",
                "project_dir": "working_directory",
                "cmd": "command",  # Common mistake
                "maven_command": "command",
            },
            "manage_context": {
                "type": "action",
                "operation": "action",
                "context_type": "action",
                "name": "task_id",
                "id": "task_id",
                "target": "action",  # Map target to action for switch-like operations
                "switch": "action",  # Map switch to action
                "task_name": "task_id",
                "branch_name": "task_id",
                # CRITICAL FIX: Map content-related parameters to 'entry' for add_context action
                "description": "entry",  # Fixed: was incorrectly mapped to 'summary'
                "content": "entry",
                "data": "entry",
                "info": "entry",
                "details": "entry",
                "context": "entry",
                "observation": "entry",
                "result": "entry",
                # For complete_task action, these should map to summary
                "completion_summary": "summary",
                "task_summary": "summary",
                "results": "summary",
            },
        }

        # Apply tool-specific mappings first (higher priority)
        if tool_name in tool_specific_mappings:
            tool_mappings = tool_specific_mappings[tool_name]
            for old_name, new_name in tool_mappings.items():
                if old_name in fixed_params and new_name in properties:
                    old_value = fixed_params[old_name]
                    # If target parameter exists but old parameter has a non-default value, use the old value
                    if new_name in fixed_params:
                        # Check if the existing value is a default/placeholder value
                        existing_value = fixed_params[new_name]
                        if (
                            existing_value in ["help", "", None]
                            or str(existing_value).strip() == ""
                            or (
                                isinstance(existing_value, str)
                                and len(old_value) > len(existing_value)
                            )
                        ):
                            fixed_params[new_name] = old_value
                            self._add_parameter_fix(
                                fixes,
                                field=new_name,
                                before=existing_value,
                                after=old_value,
                                reason=f"Renamed parameter '{old_name}' to '{new_name}'",
                                source="schema_alias",
                            )
                            self.logger.info(
                                f"🔧 Tool-specific rename (override): '{old_name}' → '{new_name}' for {tool_name}"
                            )
                        else:
                            self._add_parameter_fix(
                                fixes,
                                field=old_name,
                                before=old_value,
                                after=None,
                                reason=f"Removed alias '{old_name}' because '{new_name}' already had a value",
                                source="schema_alias",
                            )
                            self.logger.debug(
                                f"🔧 Skipping rename '{old_name}' → '{new_name}' (target has value: {existing_value})"
                            )
                    else:
                        # Target doesn't exist, normal mapping
                        fixed_params[new_name] = old_value
                        self._add_parameter_fix(
                            fixes,
                            field=new_name,
                            before=None,
                            after=old_value,
                            reason=f"Renamed parameter '{old_name}' to '{new_name}'",
                            source="schema_alias",
                        )
                        self.logger.info(
                            f"🔧 Tool-specific rename: '{old_name}' → '{new_name}' for {tool_name}"
                        )

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
                    new_value = old_value[new_name]
                    fixed_params[new_name] = new_value
                    self.logger.info(
                        f"🔧 Extracted nested value from '{old_name}' to '{new_name}' for {tool_name}"
                    )
                else:
                    new_value = old_value
                    fixed_params[new_name] = new_value
                    self.logger.info(
                        f"🔧 Renamed parameter '{old_name}' to '{new_name}' for {tool_name}"
                    )

                self._add_parameter_fix(
                    fixes,
                    field=new_name,
                    before=None,
                    after=new_value,
                    reason=f"Renamed parameter '{old_name}' to '{new_name}'",
                    source="schema_alias",
                )
                del fixed_params[old_name]
                mappings_applied.append(f"{old_name} → {new_name}")

        # Log all mappings applied for debugging
        if mappings_applied:
            self.logger.debug(
                f"Parameter mappings applied for {tool_name}: {', '.join(mappings_applied)}"
            )

        return fixed_params

    def _apply_basic_parameter_fixes(
        self,
        tool_name: str,
        params: Dict[str, Any],
        parameter_fixes: Optional[list[ParameterFix]] = None,
    ) -> Dict[str, Any]:
        """Apply basic parameter fixes when schema is not available."""
        fixes = parameter_fixes if parameter_fixes is not None else []
        fixed_params = params.copy()

        # Tool-specific basic fixes
        if tool_name == "maven":
            if not fixed_params.get("command"):
                before = fixed_params.get("command")
                fixed_params["command"] = "compile"
                self._add_parameter_fix(
                    fixes,
                    field="command",
                    before=before,
                    after="compile",
                    reason="Added default Maven command",
                    source="default",
                )
        elif tool_name == "bash":
            if not fixed_params.get("command"):
                before = fixed_params.get("command")
                fixed_params["command"] = "pwd"  # Safe default
                self._add_parameter_fix(
                    fixes,
                    field="command",
                    before=before,
                    after="pwd",
                    reason="Added default bash command",
                    source="default",
                )
        elif tool_name == "file_io":
            if not fixed_params.get("action"):
                before = fixed_params.get("action")
                fixed_params["action"] = "read"
                self._add_parameter_fix(
                    fixes,
                    field="action",
                    before=before,
                    after="read",
                    reason="Added default file_io action",
                    source="default",
                )
            if not fixed_params.get("file_path") and fixed_params.get("action") == "read":
                before = fixed_params.get("file_path")
                fixed_params["file_path"] = "/workspace"
                self._add_parameter_fix(
                    fixes,
                    field="file_path",
                    before=before,
                    after="/workspace",
                    reason="Added default file path for read action",
                    source="default",
                )
        elif tool_name == "manage_context":
            if not fixed_params.get("action"):
                before = fixed_params.get("action")
                fixed_params["action"] = "get_info"
                self._add_parameter_fix(
                    fixes,
                    field="action",
                    before=before,
                    after="get_info",
                    reason="Added default manage_context action",
                    source="default",
                )
        elif tool_name == "project_setup":
            if not fixed_params.get("action"):
                before = fixed_params.get("action")
                # If we have a repository URL, default to clone
                if self.repository_url:
                    fixed_params["action"] = "clone"
                    self._add_parameter_fix(
                        fixes,
                        field="action",
                        before=before,
                        after="clone",
                        reason="Added default project_setup action",
                        source="default",
                    )
                    repo_before = fixed_params.get("repository_url")
                    fixed_params["repository_url"] = self.repository_url
                    self._add_parameter_fix(
                        fixes,
                        field="repository_url",
                        before=repo_before,
                        after=self.repository_url,
                        reason="Injected repository URL from orchestrator state",
                        source="state_injection",
                    )
                else:
                    fixed_params["action"] = "detect_project_type"
                    self._add_parameter_fix(
                        fixes,
                        field="action",
                        before=before,
                        after="detect_project_type",
                        reason="Added default project_setup action",
                        source="default",
                    )
        elif tool_name == "web_search":
            if not fixed_params.get("query"):
                before = fixed_params.get("query")
                fixed_params["query"] = "help"
                self._add_parameter_fix(
                    fixes,
                    field="query",
                    before=before,
                    after="help",
                    reason="Added default web_search query",
                    source="default",
                )

        return fixed_params

    def _apply_tool_specific_fixes(
        self,
        tool_name: str,
        params: Dict[str, Any],
        parameter_fixes: Optional[list[ParameterFix]] = None,
    ) -> Dict[str, Any]:
        """Apply tool-specific parameter fixes using state memory."""
        fixes = parameter_fixes if parameter_fixes is not None else []
        fixed_params = params.copy()

        if tool_name == "project_setup":
            # Auto-inject repository URL if available and action is clone
            if fixed_params.get("action") == "clone" and not fixed_params.get("repository_url"):
                if self.repository_url:
                    before = fixed_params.get("repository_url")
                    fixed_params["repository_url"] = self.repository_url
                    self._add_parameter_fix(
                        fixes,
                        field="repository_url",
                        before=before,
                        after=self.repository_url,
                        reason="Injected repository URL from orchestrator state",
                        source="state_injection",
                    )
                    self.logger.info(f"🔧 Auto-injected repository URL: {self.repository_url}")

            # CRITICAL FIX: Handle target_directory correctly for workspace vs fallback modes
            if fixed_params.get("action") == "clone":
                # Check current workspace status
                is_fallback_mode = self.successful_states.get("workspace_fallback", False)
                current_workdir = self.successful_states.get("working_directory", "/workspace")

                if is_fallback_mode:
                    # We're in abnormal fallback mode - need to specify full path
                    fallback_reason = self.successful_states.get(
                        "fallback_reason", "Unknown reason"
                    )

                    self.logger.error(f"🚨 CLONE IN FALLBACK MODE: Using {current_workdir}")
                    self.logger.error(f"🚨 Reason: {fallback_reason}")
                    self.logger.error("🚨 This is SUBOPTIMAL - clone should happen in /workspace")

                    # For fallback mode, we need to specify the full path
                    if not fixed_params.get("target_directory"):
                        # Extract project name from URL
                        repo_name = (
                            fixed_params.get("repository_url", "")
                            .split("/")[-1]
                            .replace(".git", "")
                        )
                        before = fixed_params.get("target_directory")
                        if repo_name:
                            fallback_target = f"{current_workdir}/{repo_name}"
                            fixed_params["target_directory"] = fallback_target
                            self._add_parameter_fix(
                                fixes,
                                field="target_directory",
                                before=before,
                                after=fallback_target,
                                reason="Injected fallback clone target from current working directory",
                                source="state_injection",
                            )
                            self.logger.error(
                                f"🚨 Setting fallback clone target: {fallback_target}"
                            )
                        else:
                            # Use fallback directory as-is
                            fixed_params["target_directory"] = current_workdir
                            self._add_parameter_fix(
                                fixes,
                                field="target_directory",
                                before=before,
                                after=current_workdir,
                                reason="Injected fallback clone target from current working directory",
                                source="state_injection",
                            )
                            self.logger.error(
                                f"🚨 Using fallback directory directly: {current_workdir}"
                            )
                else:
                    # Normal case - workspace is available
                    self.logger.info("✅ CLONE IN WORKSPACE: Standard workspace cloning")

                    # CRITICAL FIX: Don't set target_directory to /workspace!
                    # Let project_setup tool auto-generate the project subdirectory name
                    if fixed_params.get("target_directory") == "/workspace":
                        # Remove the incorrect target_directory - let tool auto-generate
                        before = fixed_params["target_directory"]
                        del fixed_params["target_directory"]
                        self._add_parameter_fix(
                            fixes,
                            field="target_directory",
                            before=before,
                            after=None,
                            reason="Removed workspace root clone target so project_setup can create a subdirectory",
                            source="safety_fix",
                        )
                        self.logger.info(
                            "🔧 Removed incorrect target_directory, will auto-generate project subdirectory"
                        )
                    elif not fixed_params.get("target_directory"):
                        # No target_directory specified - this is correct, tool will auto-generate
                        self.logger.info(
                            "✅ No target_directory specified - project_setup will create subdirectory"
                        )
                    else:
                        # Explicit target_directory specified
                        target_dir = fixed_params["target_directory"]
                        if not target_dir.startswith("/workspace/"):
                            self.logger.warning(f"⚠️ EXPLICIT NON-WORKSPACE CLONE: {target_dir}")
                            self.logger.warning("⚠️ This may cause project layout issues")
                        else:
                            self.logger.info(f"✅ Workspace subdirectory clone: {target_dir}")

            # Prevent duplicate cloning
            cloned_repos = self.successful_states.get("cloned_repos", set())
            if (
                fixed_params.get("action") == "clone"
                and fixed_params.get("repository_url") in cloned_repos
            ):
                before = fixed_params.get("action")
                self.logger.warning(
                    "🔧 Repository already cloned, changing action to detect_project_type"
                )
                fixed_params["action"] = "detect_project_type"
                self._add_parameter_fix(
                    fixes,
                    field="action",
                    before=before,
                    after="detect_project_type",
                    reason="Avoided duplicate clone for already cloned repository",
                    source="safety_fix",
                )

        elif tool_name == "maven":
            # Ensure maven has a valid command
            if not fixed_params.get("command") or fixed_params.get("command").strip() == "":
                before = fixed_params.get("command")
                # Use intelligent default based on current state
                if self.successful_states.get("maven_success"):
                    fixed_params["command"] = "test"  # If compile succeeded before, try test
                else:
                    fixed_params["command"] = "compile"  # Start with compile
                self._add_parameter_fix(
                    fixes,
                    field="command",
                    before=before,
                    after=fixed_params["command"],
                    reason="Added default Maven command based on successful state",
                    source="default",
                )

            # Auto-inject successful working directory for Maven operations
            if "working_directory" not in fixed_params:
                if self.successful_states.get("working_directory"):
                    before = fixed_params.get("working_directory")
                    fixed_params["working_directory"] = self.successful_states["working_directory"]
                    self._add_parameter_fix(
                        fixes,
                        field="working_directory",
                        before=before,
                        after=self.successful_states["working_directory"],
                        reason="Injected working directory from successful state",
                        source="state_injection",
                    )
                    self.logger.info(
                        f"🔧 Auto-injected successful working directory: {self.successful_states['working_directory']}"
                    )
                else:
                    # Try to infer from repository URL
                    if self.repository_url:
                        repo_name = self.repository_url.split("/")[-1].replace(".git", "")
                        inferred_workdir = f"/workspace/{repo_name}"
                        before = fixed_params.get("working_directory")
                        fixed_params["working_directory"] = inferred_workdir
                        self._add_parameter_fix(
                            fixes,
                            field="working_directory",
                            before=before,
                            after=inferred_workdir,
                            reason="Inferred working directory from repository URL",
                            source="state_injection",
                        )
                        self.logger.info(
                            f"🔧 Inferred working directory from repo: /workspace/{repo_name}"
                        )

            # Convert common typos
            command = fixed_params.get("command", "")
            if command in ["test", "tests"]:
                fixed_params["command"] = "test"
            elif command in ["build", "compile"]:
                fixed_params["command"] = "compile"
            elif command in ["install", "package"]:
                fixed_params["command"] = "package"
            self._add_parameter_fix(
                fixes,
                field="command",
                before=command,
                after=fixed_params.get("command"),
                reason="Normalized Maven command alias",
                source="safety_fix",
            )

        elif tool_name == "bash":
            # Ensure bash has a command
            if not fixed_params.get("command") or fixed_params.get("command").strip() == "":
                before = fixed_params.get("command")
                fixed_params["command"] = "pwd"
                self._add_parameter_fix(
                    fixes,
                    field="command",
                    before=before,
                    after="pwd",
                    reason="Added default bash command",
                    source="default",
                )

            # Auto-inject successful working directory for bash operations
            if "working_directory" not in fixed_params:
                before = fixed_params.get("working_directory")
                if self.successful_states.get("working_directory"):
                    fixed_params["working_directory"] = self.successful_states["working_directory"]
                    self._add_parameter_fix(
                        fixes,
                        field="working_directory",
                        before=before,
                        after=self.successful_states["working_directory"],
                        reason="Injected working directory from successful state",
                        source="state_injection",
                    )
                    self.logger.info(
                        f"🔧 Auto-injected successful working directory: {self.successful_states['working_directory']}"
                    )
                else:
                    fixed_params["working_directory"] = "/workspace"
                    self._add_parameter_fix(
                        fixes,
                        field="working_directory",
                        before=before,
                        after="/workspace",
                        reason="Injected default workspace working directory",
                        source="state_injection",
                    )

            command_str = fixed_params.get("command", "")
            rewritten_command, added_fail_at_end = self._append_maven_fail_at_end_if_needed(
                command_str
            )
            if added_fail_at_end:
                before = command_str
                fixed_params["command"] = rewritten_command
                self._add_parameter_fix(
                    fixes,
                    field="command",
                    before=before,
                    after=fixed_params["command"],
                    reason="Appended Maven fail-at-end flag to bash command",
                    source="safety_fix",
                )
                self.logger.info("🔧 Appended --fail-at-end to bash Maven command")

        elif tool_name == "file_io":
            # Ensure file_io has an action
            if not fixed_params.get("action"):
                before = fixed_params.get("action")
                fixed_params["action"] = "read"
                self._add_parameter_fix(
                    fixes,
                    field="action",
                    before=before,
                    after="read",
                    reason="Added default file_io action",
                    source="default",
                )

            # CRITICAL PRIORITY: Use workspace paths when possible, warn about fallbacks
            current_workdir = self.successful_states.get("working_directory", "/workspace")
            is_fallback_mode = self.successful_states.get("workspace_fallback", False)

            # If reading but no file path, default to current directory listing
            if fixed_params.get("action") == "read" and not fixed_params.get("path"):
                action_before = fixed_params.get("action")
                path_before = fixed_params.get("path")
                fixed_params["action"] = "list"
                fixed_params["path"] = current_workdir
                self._add_parameter_fix(
                    fixes,
                    field="action",
                    before=action_before,
                    after="list",
                    reason="Switched read without path to directory listing",
                    source="safety_fix",
                )
                self._add_parameter_fix(
                    fixes,
                    field="path",
                    before=path_before,
                    after=current_workdir,
                    reason="Injected current working directory for file listing",
                    source="state_injection",
                )

                if is_fallback_mode:
                    self.logger.error(
                        f"🚨 FILE_IO FALLBACK: Listing {current_workdir} (not in workspace)"
                    )
                else:
                    self.logger.info(f"✅ FILE_IO WORKSPACE: Listing {current_workdir}")

            # If path is relative and we have a known working directory, make it absolute
            elif fixed_params.get("path") and not fixed_params["path"].startswith("/"):
                relative_path = fixed_params["path"]
                absolute_path = f"{current_workdir}/{relative_path}"
                fixed_params["path"] = absolute_path
                self._add_parameter_fix(
                    fixes,
                    field="path",
                    before=relative_path,
                    after=absolute_path,
                    reason="Resolved relative file path against current working directory",
                    source="safety_fix",
                )

                if is_fallback_mode:
                    self.logger.error(
                        f"🚨 FILE_IO FALLBACK PATH: {relative_path} → {absolute_path} (not in workspace)"
                    )
                else:
                    self.logger.info(
                        f"✅ FILE_IO WORKSPACE PATH: {relative_path} → {absolute_path}"
                    )

            # PRIORITY CHECK: If path points to /workspace but we're in fallback mode, this is concerning
            elif fixed_params.get("path") and fixed_params["path"].startswith("/workspace"):
                if is_fallback_mode and not current_workdir.startswith("/workspace"):
                    original_path = fixed_params["path"]
                    self.logger.error(
                        f"🚨 FILE_IO MISMATCH: Requesting {original_path} but workspace unavailable"
                    )
                    self.logger.error(f"🚨 Current fallback directory: {current_workdir}")

                    # Try to map /workspace/... to current_workdir/...
                    relative_part = original_path.replace("/workspace", "").lstrip("/")
                    if relative_part:
                        adjusted_path = f"{current_workdir}/{relative_part}"
                        self.logger.error(
                            f"🚨 ATTEMPTING PATH MAPPING: {original_path} → {adjusted_path}"
                        )
                        self.logger.error("🚨 This may fail if files are actually in /workspace")
                    else:
                        adjusted_path = current_workdir
                        self.logger.error(f"🚨 MAPPING WORKSPACE ROOT to fallback: {adjusted_path}")

                    fixed_params["path"] = adjusted_path
                    self._add_parameter_fix(
                        fixes,
                        field="path",
                        before=original_path,
                        after=adjusted_path,
                        reason="Mapped workspace path to fallback working directory",
                        source="safety_fix",
                    )
                else:
                    # Normal case - workspace path and we're in workspace
                    if not is_fallback_mode:
                        self.logger.debug(f"✅ FILE_IO WORKSPACE: Accessing {fixed_params['path']}")
                    else:
                        self.logger.info(
                            f"✅ FILE_IO WORKSPACE: Accessing {fixed_params['path']} (workspace available)"
                        )

            # If we're in fallback mode, warn about any non-fallback paths
            elif is_fallback_mode and fixed_params.get("path"):
                path = fixed_params["path"]
                if not path.startswith(current_workdir):
                    self.logger.warning(
                        f"⚠️ FILE_IO OUTSIDE FALLBACK: Accessing {path} while in fallback mode ({current_workdir})"
                    )
                    self.logger.warning("⚠️ This may fail if the path doesn't exist")

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
                "reduce": "compact_context",
            }

            if action in action_aliases:
                original_action = action
                fixed_params["action"] = action_aliases[action]
                self._add_parameter_fix(
                    fixes,
                    field="action",
                    before=original_action,
                    after=action_aliases[action],
                    reason="Normalized manage_context action alias",
                    source="safety_fix",
                )
                self.logger.info(
                    f"🔧 Converted action '{original_action}' to '{action_aliases[action]}' for manage_context"
                )

                # Add default summary for completion actions
                if action_aliases[action] == "complete_task" and not fixed_params.get("summary"):
                    summary_before = fixed_params.get("summary")
                    if action in ["failure", "failed"]:
                        fixed_params["summary"] = (
                            "Task failed to complete successfully due to encountered issues"
                        )
                    else:
                        fixed_params["summary"] = "Task completed with mixed results"
                    self._add_parameter_fix(
                        fixes,
                        field="summary",
                        before=summary_before,
                        after=fixed_params["summary"],
                        reason="Added default summary for complete_task action",
                        source="default",
                    )
                    self.logger.info("🔧 Added default summary for complete_task action")
            elif action == "switch_to_trunk":
                # This is correct, but ensure we have a summary if needed
                if not fixed_params.get("summary"):
                    before = fixed_params.get("summary")
                    fixed_params["summary"] = "Switching back to trunk context"
                    self._add_parameter_fix(
                        fixes,
                        field="summary",
                        before=before,
                        after="Switching back to trunk context",
                        reason="Added default summary for switch_to_trunk action",
                        source="default",
                    )
                    self.logger.info("🔧 Added default summary for switch_to_trunk action")

            # Ensure required parameters for create_branch
            if fixed_params.get("action") == "create_branch":
                if not fixed_params.get("task_id"):
                    # Generate a default task_id if missing
                    before = fixed_params.get("task_id")
                    summary = fixed_params.get("summary", "default_task")
                    task_id = summary.replace(" ", "_").lower()[:20]
                    fixed_params["task_id"] = task_id
                    self._add_parameter_fix(
                        fixes,
                        field="task_id",
                        before=before,
                        after=task_id,
                        reason="Generated missing task_id from summary",
                        source="default",
                    )
                    self.logger.info(f"🔧 Generated missing task_id: {task_id}")

            # For start_task, ensure we have task_id
            elif fixed_params.get("action") == "start_task":
                if not fixed_params.get("task_id"):
                    # Auto-inject the correct next task ID based on context
                    before = fixed_params.get("task_id")
                    fixed_params["task_id"] = "task_1"  # Default to first task
                    self._add_parameter_fix(
                        fixes,
                        field="task_id",
                        before=before,
                        after="task_1",
                        reason="Added default task_id for start_task",
                        source="default",
                    )
                    self.logger.info("🔧 Auto-injected default task_id: task_1 for start_task")

            # For complete_task, ensure we have summary
            elif fixed_params.get("action") == "complete_task":
                if not fixed_params.get("summary"):
                    before = fixed_params.get("summary")
                    fixed_params["summary"] = "Task completed with mixed results"
                    self._add_parameter_fix(
                        fixes,
                        field="summary",
                        before=before,
                        after="Task completed with mixed results",
                        reason="Added default summary for complete_task action",
                        source="default",
                    )
                    self.logger.info("🔧 Added default summary for complete_task action")

        return fixed_params

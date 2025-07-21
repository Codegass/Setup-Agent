"""Bash tool for executing shell commands with specialized grep functionality."""

import shlex
import subprocess
from typing import Any, Dict

from loguru import logger

from .base import BaseTool, ToolResult


class BashTool(BaseTool):
    """Tool for executing bash commands with advanced grep investigation capabilities.
    
    GREP - THE PRIMARY INVESTIGATION TOOL:
    grep is the most powerful tool for code investigation. Use it to:
    • Find function definitions: grep -r "def function_name" .
    • Search for class declarations: grep -r "class ClassName" .
    • Find imports: grep -r "import module_name" .
    • Search for specific patterns: grep -r "error\|exception\|fail" .
    • Find configuration: grep -r "config\|setting" .
    
    ESSENTIAL GREP PATTERNS:
    • Basic search: grep "pattern" file.txt
    • Recursive search: grep -r "pattern" directory/
    • Case insensitive: grep -i "pattern" file.txt
    • Show line numbers: grep -n "pattern" file.txt
    • Show context: grep -C 3 "pattern" file.txt (3 lines before/after)
    • Multiple patterns: grep -E "pattern1|pattern2" file.txt
    • Exclude files: grep -r "pattern" . --exclude="*.log"
    • Include only specific files: grep -r "pattern" . --include="*.py"
    • Count matches: grep -c "pattern" file.txt
    • Show only matching files: grep -l "pattern" *.txt
    • Invert match: grep -v "pattern" file.txt
    
    INVESTIGATION WORKFLOWS:
    1. Project Overview: grep -r "def\|class\|import" . --include="*.py" | head -20
    2. Error Investigation: grep -r -i "error\|exception\|fail" . --include="*.py" -C 2
    3. Configuration Discovery: grep -r "config\|setting\|env" . --exclude-dir=".git"
    4. API Endpoints: grep -r "route\|endpoint\|@app" . --include="*.py"
    5. Database Queries: grep -r "SELECT\|INSERT\|UPDATE\|DELETE" . -i
    
    ADVANCED GREP TECHNIQUES:
    • Regex patterns: grep -E "^[A-Z]+_[A-Z]+" config.txt
    • Fixed strings (no regex): grep -F "literal.string" file.txt
    • Whole words only: grep -w "word" file.txt
    • Binary files: grep -a "pattern" binary_file
    • Follow symlinks: grep -r -L "pattern" directory/
    
    Use bash for: file operations, package installation, git operations, system tasks, and ESPECIALLY grep-based code investigation.
    """

    def __init__(self, docker_orchestrator=None):
        super().__init__(
            name="bash",
            description="Execute shell commands in the container. SPECIALIZES in grep-based code investigation. "
            "grep is your PRIMARY tool for understanding codebases, finding patterns, and investigating issues. "
            "Use for file operations, package installation, git operations, and comprehensive code analysis.",
        )
        self.docker_orchestrator = docker_orchestrator

    def _extract_key_info(self, output: str, tool_name: str) -> str:
        """Override to use bash-specific extraction with grep result optimization."""
        if tool_name == "bash" or tool_name == self.name:
            return self._extract_bash_key_info(output)
        return output

    def _extract_bash_key_info(self, output: str) -> str:
        """Extract key information from bash output, especially optimized for grep results."""
        if not output:
            return output
            
        lines = output.split('\n')
        
        # If this looks like grep output, preserve more context
        if any(line.strip() and ':' in line for line in lines[:10]):
            # This might be grep output with file:line:content format
            key_lines = []
            for line in lines:
                if line.strip():
                    # Preserve grep results with context
                    key_lines.append(line)
                    if len(key_lines) >= 50:  # Keep more grep results
                        break
            
            if key_lines:
                result = '\n'.join(key_lines)
                if len(lines) > len(key_lines):
                    result += f"\n... [Showing first {len(key_lines)} matches out of {len(lines)} total lines]"
                return result
        
        # For other bash commands, use general extraction
        key_lines = []
        error_lines = []
        
        for line in lines:
            line_lower = line.lower()
            
            # Capture errors and important status
            if any(keyword in line_lower for keyword in [
                'error:', 'exception:', 'failed:', 'warning:', 'critical:',
                'success:', 'completed:', 'installed:', 'removed:', 'updated:'
            ]):
                if 'error' in line_lower or 'exception' in line_lower or 'failed' in line_lower:
                    error_lines.append(f"🚨 {line.strip()}")
                else:
                    key_lines.append(f"✅ {line.strip()}")
            
            # File operations
            elif any(keyword in line_lower for keyword in [
                'created:', 'copied:', 'moved:', 'deleted:', 'modified:'
            ]):
                key_lines.append(f"📁 {line.strip()}")
            
            # Package management
            elif any(keyword in line_lower for keyword in [
                'installing', 'removing', 'upgrading', 'package'
            ]):
                key_lines.append(f"📦 {line.strip()}")
            
            # Git operations
            elif any(keyword in line_lower for keyword in [
                'commit', 'push', 'pull', 'branch', 'merge'
            ]):
                key_lines.append(f"🔄 {line.strip()}")
        
        # Combine results
        result_lines = error_lines + key_lines
        if result_lines:
            return '\n'.join(result_lines)
        
        # If no key patterns found, return truncated original
        if len(output) > 2000:
            return output[:1000] + "\n... [Content truncated] ..." + output[-500:]
        
        return output

    def execute(self, command: str, timeout: int = 60, working_directory: str = None) -> ToolResult:
        """Execute a bash command with enhanced grep support."""
        if not command.strip():
            return ToolResult(success=False, output="", error="Empty command provided")

        # Enhance grep commands with helpful flags
        enhanced_command = self._enhance_grep_command(command)
        
        logger.debug(f"Executing bash command: {enhanced_command}")
        if working_directory:
            logger.debug(f"Working directory: {working_directory}")

        try:
            # Use docker orchestrator if available
            if self.docker_orchestrator:
                result = self.docker_orchestrator.execute_command(enhanced_command, workdir=working_directory)
                
                return ToolResult(
                    success=result["exit_code"] == 0,
                    output=result["output"],
                    error=None if result["exit_code"] == 0 else f"Command failed with exit code {result['exit_code']}",
                    metadata={
                        "exit_code": result["exit_code"], 
                        "command": enhanced_command, 
                        "original_command": command,
                        "timeout": timeout,
                        "is_grep": "grep" in command.lower()
                    },
                )
            else:
                # Fallback to local execution
                result = subprocess.run(
                    enhanced_command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=working_directory or "/workspace",
                )

                # Combine stdout and stderr for complete output
                output = ""
                if result.stdout:
                    output += result.stdout
                if result.stderr:
                    if output:
                        output += "\n--- STDERR ---\n"
                    output += result.stderr

                success = result.returncode == 0

                return ToolResult(
                    success=success,
                    output=output,
                    error=None if success else f"Command failed with exit code {result.returncode}",
                    metadata={
                        "exit_code": result.returncode, 
                        "command": enhanced_command,
                        "original_command": command, 
                        "timeout": timeout,
                        "is_grep": "grep" in command.lower()
                    },
                )

        except subprocess.TimeoutExpired:
            error_msg = f"Command timed out after {timeout} seconds"
            logger.warning(f"Bash command timeout: {enhanced_command}")
            return ToolResult(
                success=False,
                output="",
                error=error_msg,
                metadata={"timeout": timeout, "command": enhanced_command, "original_command": command},
            )

        except Exception as e:
            error_msg = f"Failed to execute command: {str(e)}"
            logger.error(f"Bash command execution error: {error_msg}")
            return ToolResult(
                success=False, 
                output="", 
                error=error_msg, 
                metadata={"command": enhanced_command, "original_command": command}
            )

    def _enhance_grep_command(self, command: str) -> str:
        """Enhance grep commands with helpful default flags."""
        if not command.strip().startswith('grep'):
            return command
        
        # Parse the command to avoid double-adding flags
        parts = shlex.split(command)
        if len(parts) < 2:
            return command
        
        # Check if useful flags are already present
        has_recursive = '-r' in parts or '--recursive' in parts
        has_line_numbers = '-n' in parts or '--line-number' in parts
        has_color = '--color' in parts
        
        enhanced_parts = [parts[0]]  # Start with 'grep'
        
        # Add helpful flags if not present
        if not has_line_numbers:
            enhanced_parts.append('-n')
        if not has_color:
            enhanced_parts.append('--color=always')
        
        # Add the rest of the command
        enhanced_parts.extend(parts[1:])
        
        enhanced_command = ' '.join(shlex.quote(part) if ' ' in part else part for part in enhanced_parts)
        
        # If the command seems to be searching in current directory without -r, suggest it
        if not has_recursive and (enhanced_command.endswith(' .') or enhanced_command.endswith(' ./')):
            logger.info("💡 TIP: Consider using 'grep -r' for recursive search in directories")
        
        return enhanced_command

    def get_grep_examples(self) -> str:
        """Get comprehensive grep usage examples."""
        return """
GREP INVESTIGATION EXAMPLES:

🔍 BASIC SEARCHES:
• Find function: grep -rn "def my_function" .
• Find class: grep -rn "class MyClass" .
• Find imports: grep -rn "import requests" .
• Case insensitive: grep -rni "error" .

🎯 PATTERN MATCHING:
• Multiple patterns: grep -rn "error\|exception\|fail" .
• Regex pattern: grep -rn "def [a-z_]*test" .
• Whole words: grep -rnw "test" .
• Start of line: grep -rn "^class " .

📁 FILE FILTERING:
• Python files only: grep -rn "pattern" . --include="*.py"
• Exclude logs: grep -rn "pattern" . --exclude="*.log"
• Exclude directories: grep -rn "pattern" . --exclude-dir=".git"

🔬 CONTEXT & DETAILS:
• Show context: grep -rn -C 3 "error" .  (3 lines before/after)
• Count matches: grep -rc "pattern" .
• List files only: grep -rl "pattern" .
• Invert match: grep -rnv "pattern" .

🚀 ADVANCED INVESTIGATIONS:
• Find all APIs: grep -rn "@app\|@route\|def.*api" . --include="*.py"
• Database queries: grep -rni "select\|insert\|update\|delete" .
• Configuration: grep -rn "config\|setting\|env" . --exclude-dir=".git"
• Error handling: grep -rn "try:\|except\|raise" . --include="*.py" -A 2
• Find TODOs: grep -rn "TODO\|FIXME\|HACK" .

💡 PRO TIPS:
• Use -C 2 to see context around matches
• Combine with head/tail: grep -rn "pattern" . | head -20
• Use --color=always for better readability
• Save results: grep -rn "pattern" . > search_results.txt
        """

    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool."""
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string", 
                    "description": "The bash command to execute. For grep investigations, use patterns like: "
                    "'grep -rn \"pattern\" .' for recursive search, "
                    "'grep -rni \"error|exception\" . --include=\"*.py\"' for specific file types, "
                    "'grep -rn -C 3 \"function_name\" .' for context around matches. "
                    "See get_grep_examples() for comprehensive patterns."
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 60)",
                    "default": 60,
                },
                "working_directory": {
                    "type": "string",
                    "description": "Working directory for command execution (default: /workspace)",
                    "default": None,
                },
            },
            "required": ["command"],
        }

    def get_usage_example(self) -> str:
        """Get usage examples focused on grep investigations."""
        return f"""
{self.name}(command="grep -rn 'def process_data' . --include='*.py'")  # Find function definitions
{self.name}(command="grep -rni 'error|exception' . --include='*.py' -C 2")  # Find error handling with context
{self.name}(command="grep -rn 'import pandas' .")  # Find specific imports
{self.name}(command="ls -la")  # Standard file operations
{self.name}(command="git status")  # Git operations

💡 For comprehensive grep patterns, use: get_grep_examples()
        """

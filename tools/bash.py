"""Bash tool for executing shell commands with specialized grep functionality."""

import shlex
import subprocess
from typing import Any, Dict, Optional

from loguru import logger

from .base import BaseTool, ToolResult, ToolError


class BashTool(BaseTool):
    """Tool for executing bash commands with advanced grep investigation capabilities.
    
    GREP - THE PRIMARY INVESTIGATION TOOL:
    grep is the most powerful tool for code investigation. Use it to:
    • Find function definitions: grep -r "def function_name" .
    • Search for class declarations: grep -r "class ClassName" .
    • Find imports: grep -r "import module_name" .
    • Search for specific patterns: grep -r "error\\|exception\\|fail" .
    • Find configuration: grep -r "config\\|setting" .
    
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
    1. Project Overview: grep -r "def\\|class\\|import" . --include="*.py" | head -20
    2. Error Investigation: grep -r -i "error\\|exception\\|fail" . --include="*.py" -C 2
    3. Configuration Discovery: grep -r "config\\|setting\\|env" . --exclude-dir=".git"
    4. API Endpoints: grep -r "route\\|endpoint\\|@app" . --include="*.py"
    5. Database Queries: grep -r "SELECT\\|INSERT\\|UPDATE\\|DELETE" . -i
    
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
            # Note: We can't access the original command here, so this is a fallback
            return self._extract_bash_key_info(output, "")
        return output

    def _extract_bash_key_info(self, output: str, command: str = "") -> str:
        """Extract key information from bash output with aggressive truncation for verbose commands."""
        if not output:
            return output
            
        lines = output.split('\n')
        total_lines = len(lines)
        
        # CRITICAL: Detect verbose package management commands by COMMAND, not output
        command_lower = command.lower()
        is_verbose_package_cmd = any(pattern in command_lower for pattern in [
            'apt-get install', 'apt install', 'yum install', 'dnf install',
            'npm install', 'pip install', 'cargo install', 'go get'
        ])
        
        if is_verbose_package_cmd and total_lines > 50:
            # For verbose package commands, use AGGRESSIVE truncation
            logger.info(f"🔧 Detected verbose package command with {total_lines} lines, applying aggressive truncation")
            
            # Keep only: head (25 lines) + tail (25 lines) = 50 lines total
            key_start = lines[:25]
            key_end = lines[-25:]
            
            # Extract critical status lines from the middle if any
            critical_lines = []
            for line in lines[10:-10]:  # Skip already included start/end
                line_lower = line.lower()
                if any(critical in line_lower for critical in [
                    'error:', 'failed:', 'could not', 'unable to', 'permission denied',
                    'build success', 'build failure', 'completed successfully',
                    'warning:', 'critical:'
                ]):
                    critical_lines.append(line)
                    if len(critical_lines) >= 5:  # Limit critical lines to prevent spam
                        break
            
            result_parts = []
            result_parts.extend(key_start)
            if critical_lines:
                result_parts.append(f"\n... [Key status messages from {total_lines} lines] ...")
                result_parts.extend(critical_lines)
            result_parts.append(f"\n... [Skipped {total_lines - 50 - len(critical_lines)} lines of verbose output] ...")
            result_parts.extend(key_end)
            
            return '\n'.join(result_parts)
        
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
        
        # For regular commands, extract key information
        key_lines = []
        error_lines = []
        line_count = 0
        
        for line in lines:
            line_lower = line.lower()
            line_count += 1
            
            # Stop processing if we've seen too many lines (prevent context pollution)
            if line_count > 100:
                key_lines.append(f"... [Stopped processing after 100 lines, total: {total_lines}]")
                break
            
            # Capture errors and important status (high priority)
            if any(keyword in line_lower for keyword in [
                'error:', 'exception:', 'failed:', 'warning:', 'critical:',
                'build success', 'build failure', 'success:', 'completed:'
            ]):
                if 'error' in line_lower or 'exception' in line_lower or 'failed' in line_lower:
                    error_lines.append(f"🚨 {line.strip()}")
                else:
                    key_lines.append(f"✅ {line.strip()}")
            
            # File operations (medium priority)
            elif any(keyword in line_lower for keyword in [
                'created:', 'copied:', 'moved:', 'deleted:', 'modified:'
            ]):
                key_lines.append(f"📁 {line.strip()}")
            
            # Package management (low priority - be selective)
            elif any(keyword in line_lower for keyword in [
                'installed successfully', 'removed successfully', 'updated successfully',
                'package not found', 'dependency error'
            ]):
                key_lines.append(f"📦 {line.strip()}")
            
            # Git operations (medium priority)
            elif any(keyword in line_lower for keyword in [
                'commit', 'push', 'pull', 'branch', 'merge'
            ]):
                key_lines.append(f"🔄 {line.strip()}")
        
        # Combine results with strict limits
        result_lines = error_lines[:10] + key_lines[:30]  # Limit to prevent bloat
        if result_lines:
            if total_lines > len(result_lines) + 10:
                result_lines.append(f"... [Extracted {len(result_lines)} key lines from {total_lines} total]")
            return '\n'.join(result_lines)
        
        # If no key patterns found, apply strict truncation
        if len(output) > 1500:  # Reduced from 2000
            truncated = output[:800] + "\n... [Output truncated to prevent context pollution] ..." + output[-400:]
            logger.info(f"🔧 Applied fallback truncation: {len(output)} chars → {len(truncated)} chars")
            return truncated
        
        return output

    def execute(self, command: str, working_directory: Optional[str] = None) -> ToolResult:
        """
        Execute a bash command in the container.

        Args:
            command: The command to execute.
            working_directory: The directory to execute the command in.
        """
        if not command:
            return ToolResult.error("Command cannot be empty.", error_code="EMPTY_COMMAND")
            
        logger.info(f"Executing bash command: {command}")
        
        # Use the orchestrator to execute the command with the specified working directory
        result = self.docker_orchestrator.execute_command(command, workdir=working_directory)
        
        if result["success"]:
            # Apply our custom bash-specific extraction based on COMMAND, not output
            extracted_output = self._extract_bash_key_info(result["output"], command)
            return ToolResult(
                success=True, 
                output=extracted_output, 
                metadata={"exit_code": result["exit_code"], "original_command": command}
            )
        else:
            exit_code = result['exit_code']
            output = result['output']
            
            # Provide more specific error messages based on exit code
            if exit_code == 127:
                error_message = "Command Not Found (exit code 127)"
                suggestions = [
                    "The command was not found in the system PATH.",
                    "Check if the command is installed: which <command>",
                    "For Java tools like 'mvn', ensure JAVA_HOME and M2_HOME are set.",
                    "Try 'echo $PATH' to check the current PATH environment variable.",
                    "Install the missing command or verify the spelling.",
                ]
            elif exit_code == 126:
                error_message = "Permission Denied (exit code 126)"
                suggestions = [
                    "The command file exists but is not executable.",
                    "Try 'chmod +x <command>' to make it executable.",
                    "Check file permissions with 'ls -la <command>'.",
                ]
            elif exit_code == 2:
                error_message = "File Not Found (exit code 2)"
                suggestions = [
                    "The specified file or directory does not exist.",
                    "Check the path and filename for typos.",
                    "Use 'ls -la' to verify the file exists.",
                ]
            else:
                error_message = f"Command failed with exit code {exit_code}"
                suggestions = [
                    "Check the command syntax for errors.",
                    "Verify that the command and its arguments are correct.",
                    f"Ensure the working directory '{working_directory}' exists.",
                    "Try running a simpler command like 'ls -la' to test connectivity."
                ]
            
            if output:
                error_message += f"\nOutput:\n{output}"

            raise ToolError(
                message=error_message,
                suggestions=suggestions,
                error_code="COMMAND_FAILED",
                raw_output=output
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
• Multiple patterns: grep -rn "error\\|exception\\|fail" .
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
• Find all APIs: grep -rn "@app\\|@route\\|def.*api" . --include="*.py"
• Database queries: grep -rni "select\\|insert\\|update\\|delete" .
• Configuration: grep -rn "config\\|setting\\|env" . --exclude-dir=".git"
• Error handling: grep -rn "try:\\|except\\|raise" . --include="*.py" -A 2
• Find TODOs: grep -rn "TODO\\|FIXME\\|HACK" .

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
                    "'grep -rni \"error\\|exception\" . --include=\"*.py\"' for specific file types, "
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

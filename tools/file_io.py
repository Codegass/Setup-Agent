"""File I/O tool for reading and writing files."""

import os
from pathlib import Path
from typing import Any, Dict
import re # Added for regex operations

from loguru import logger

from .base import BaseTool, ToolResult


class FileIOTool(BaseTool):
    """Tool for file input/output operations in Docker container."""

    def __init__(self, docker_orchestrator=None):
        super().__init__(
            name="file_io",
            description="Read, write, or append to files in the Docker container. Use for editing configuration files, "
            "reading documentation, creating scripts, etc.",
        )
        self.docker_orchestrator = docker_orchestrator

    def _extract_key_info(self, output: str, tool_name: str) -> str:
        """Override to use file-io-specific extraction."""
        if tool_name == "file_io" or tool_name == self.name:
            return self._extract_file_io_key_info(output)
        return output

    def _extract_file_io_key_info(self, output: str) -> str:
        """Extract key information from file I/O output."""
        if not output or len(output) <= self.max_output_length:
            return output
        
        lines = output.split('\n')
        
        # Check if this looks like a directory listing
        if self._is_directory_listing(lines):
            return self._extract_directory_listing_info(lines, output)
        
        # Check if this looks like a large text file
        elif self._is_text_file_content(lines):
            return self._extract_file_content_info(lines, output)
        
        # For other cases, use general truncation
        return output

    def _is_directory_listing(self, lines: list) -> bool:
        """Check if output looks like a directory listing."""
        # Look for patterns typical of ls -la output
        listing_patterns = [
            r'^total \d+',  # total line from ls -la
            r'^[d-][rwx-]{9}',  # permission strings
            r'^\d+\s+',  # starts with number (some ls formats)
        ]
        
        matching_lines = 0
        for line in lines[:10]:  # Check first 10 lines
            for pattern in listing_patterns:
                if re.match(pattern, line):
                    matching_lines += 1
                    break
        
        return matching_lines >= 2

    def _is_text_file_content(self, lines: list) -> bool:
        """Check if output looks like text file content."""
        # If it doesn't look like directory listing and has many lines, assume it's file content
        return len(lines) > 50

    def _extract_directory_listing_info(self, lines: list, original_output: str) -> str:
        """Extract key info from directory listings."""
        summary = []
        
        # Count different types of files
        directories = []
        files = []
        special_files = []
        
        for line in lines:
            if re.match(r'^d[rwx-]{9}', line):  # Directory
                directories.append(line.split()[-1])  # Last part is filename
            elif re.match(r'^-[rwx-]{9}', line):  # Regular file
                filename = line.split()[-1]
                files.append(filename)
                # Check for special file types
                if any(filename.endswith(ext) for ext in ['.pom', '.xml', '.json', '.yml', '.yaml', '.properties']):
                    special_files.append(filename)
        
        summary.append(f"📁 Directory Listing Summary:")
        summary.append(f"  • {len(directories)} directories")
        summary.append(f"  • {len(files)} files")
        
        if special_files:
            summary.append(f"  • Configuration files: {', '.join(special_files[:5])}")
            if len(special_files) > 5:
                summary.append(f"    ... and {len(special_files) - 5} more config files")
        
        # Show first few items
        if directories:
            summary.append(f"\n📂 Directories (first 10): {', '.join(directories[:10])}")
            if len(directories) > 10:
                summary.append(f"    ... and {len(directories) - 10} more directories")
        
        if files:
            summary.append(f"\n📄 Files (first 15): {', '.join(files[:15])}")
            if len(files) > 15:
                summary.append(f"    ... and {len(files) - 15} more files")
        
        # Show first and last few lines of original output for context
        if len(lines) > 30:
            summary.append(f"\nFirst 15 lines of listing:")
            summary.extend(lines[:15])
            summary.append(f"\n... [truncated {len(lines) - 30} lines] ...")
            summary.append(f"\nLast 15 lines of listing:")
            summary.extend(lines[-15:])
        else:
            summary.append(f"\nFull listing:")
            summary.extend(lines)
        
        summary.append(f"\n💡 Use 'bash' with 'find' or 'grep' to search for specific files or patterns.")
        
        return '\n'.join(summary)

    def _extract_file_content_info(self, lines: list, original_output: str) -> str:
        """Extract key info from large file content."""
        summary = []
        
        summary.append(f"📄 File Content Summary ({len(lines)} lines):")
        
        # Look for key patterns in the content
        code_indicators = []
        config_indicators = []
        
        for i, line in enumerate(lines[:100]):  # Check first 100 lines
            line_lower = line.lower().strip()
            
            # Look for code patterns
            if any(pattern in line_lower for pattern in ['import ', 'package ', 'class ', 'function ', 'def ', 'public ', 'private']):
                code_indicators.append(f"Line {i+1}: {line.strip()[:80]}")
                
            # Look for config patterns
            elif any(pattern in line_lower for pattern in ['<project>', '<dependency>', '=', 'version:', 'name:']):
                config_indicators.append(f"Line {i+1}: {line.strip()[:80]}")
        
        if code_indicators:
            summary.append(f"\n🔍 Code patterns found (first 5):")
            summary.extend(code_indicators[:5])
            
        if config_indicators:
            summary.append(f"\n⚙️ Configuration patterns found (first 5):")
            summary.extend(config_indicators[:5])
        
        # Show first and last portions
        summary.append(f"\n📝 File Content (first 20 lines):")
        summary.extend(lines[:20])
        
        if len(lines) > 50:
            summary.append(f"\n... [middle content truncated, {len(lines)} total lines] ...")
            summary.append(f"\n📝 File Content (last 15 lines):")
            summary.extend(lines[-15:])
        
        summary.append(f"\n💡 Use 'bash' with 'grep' to search for specific content, or 'head'/'tail' to view portions.")
        
        return '\n'.join(summary)

    def execute(self, action: str, path: str, content: str = None) -> ToolResult:
        """Execute file I/O operation in Docker container."""
        if action not in ["read", "write", "append", "list"]:
            return ToolResult(
                success=False,
                output="",
                error=f"Invalid action '{action}'. Must be 'read', 'write', 'append', or 'list'",
            )

        # Ensure path is within workspace
        if not path.startswith("/workspace"):
            path = f"/workspace/{path.lstrip('/')}"

        try:
            if action == "read":
                return self._read_file_in_container(path)
            elif action == "write":
                return self._write_file_in_container(path, content or "")
            elif action == "append":
                return self._append_file_in_container(path, content or "")
            elif action == "list":
                return self._list_directory_in_container(path)

        except Exception as e:
            error_msg = f"File operation failed: {str(e)}"
            logger.error(f"File I/O error for {action} on {path}: {error_msg}")
            return ToolResult(
                success=False,
                output="",
                error=error_msg,
                metadata={"action": action, "path": path},
            )

    def _read_file_in_container(self, path: str) -> ToolResult:
        """Read a file from Docker container."""
        if not self.docker_orchestrator:
            return ToolResult(
                success=False,
                output="",
                error="Docker orchestrator not available for file operations",
            )
        
        try:
            # Use cat command to read file content
            result = self.docker_orchestrator.execute_command(f"cat '{path}'")
            
            if result["exit_code"] == 0:
                content = result["output"]
                logger.debug(f"Successfully read file from container: {path} ({len(content)} chars)")
                
                return ToolResult(
                    success=True,
                    output=content,
                    metadata={"action": "read", "path": path, "size": len(content)},
                )
            else:
                error_output = result.get("error", "Unknown error")
                if "No such file or directory" in error_output:
                    return ToolResult(
                        success=False,
                        output="",
                        error=f"File does not exist: {path}",
                        metadata={"action": "read", "path": path},
                    )
                else:
                    return ToolResult(
                        success=False,
                        output="",
                        error=f"Failed to read file: {error_output}",
                        metadata={"action": "read", "path": path},
                    )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Container file read failed: {str(e)}",
                metadata={"action": "read", "path": path},
            )

    def _write_file_in_container(self, path: str, content: str) -> ToolResult:
        """Write content to a file in Docker container."""
        if not self.docker_orchestrator:
            return ToolResult(
                success=False,
                output="",
                error="Docker orchestrator not available for file operations",
            )
        
        try:
            # Create parent directories if they don't exist
            parent_dir = str(Path(path).parent)
            if parent_dir != "/":
                mkdir_result = self.docker_orchestrator.execute_command(f"mkdir -p '{parent_dir}'")
                if mkdir_result["exit_code"] != 0:
                    return ToolResult(
                        success=False,
                        output="",
                        error=f"Failed to create directory {parent_dir}: {mkdir_result.get('error', 'Unknown error')}",
                    )
            
            # Escape content for shell
            escaped_content = content.replace("'", "'\"'\"'")
            write_command = f"echo '{escaped_content}' > '{path}'"
            
            result = self.docker_orchestrator.execute_command(write_command)
            
            if result["exit_code"] == 0:
                logger.debug(f"Successfully wrote file in container: {path} ({len(content)} chars)")
                
                return ToolResult(
                    success=True,
                    output=f"Successfully wrote {len(content)} characters to {path}",
                    metadata={"action": "write", "path": path, "size": len(content)},
                )
            else:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Failed to write file: {result.get('error', 'Unknown error')}",
                    metadata={"action": "write", "path": path},
                )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Container file write failed: {str(e)}",
                metadata={"action": "write", "path": path},
            )

    def _append_file_in_container(self, path: str, content: str) -> ToolResult:
        """Append content to a file in Docker container."""
        if not self.docker_orchestrator:
            return ToolResult(
                success=False,
                output="",
                error="Docker orchestrator not available for file operations",
            )
        
        try:
            # Create parent directories if they don't exist
            parent_dir = str(Path(path).parent)
            if parent_dir != "/":
                mkdir_result = self.docker_orchestrator.execute_command(f"mkdir -p '{parent_dir}'")
                if mkdir_result["exit_code"] != 0:
                    return ToolResult(
                        success=False,
                        output="",
                        error=f"Failed to create directory {parent_dir}: {mkdir_result.get('error', 'Unknown error')}",
                    )
            
            # Create file if it doesn't exist
            touch_result = self.docker_orchestrator.execute_command(f"touch '{path}'")
            if touch_result["exit_code"] != 0:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Failed to create file {path}: {touch_result.get('error', 'Unknown error')}",
                )
            
            # Escape content for shell
            escaped_content = content.replace("'", "'\"'\"'")
            append_command = f"echo '{escaped_content}' >> '{path}'"
            
            result = self.docker_orchestrator.execute_command(append_command)
            
            if result["exit_code"] == 0:
                logger.debug(f"Successfully appended to file in container: {path} ({len(content)} chars)")
                
                return ToolResult(
                    success=True,
                    output=f"Successfully appended {len(content)} characters to {path}",
                    metadata={"action": "append", "path": path, "size": len(content)},
                )
            else:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Failed to append to file: {result.get('error', 'Unknown error')}",
                    metadata={"action": "append", "path": path},
                )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Container file append failed: {str(e)}",
                metadata={"action": "append", "path": path},
            )

    def _list_directory_in_container(self, path: str) -> ToolResult:
        """List directory contents in Docker container."""
        if not self.docker_orchestrator:
            return ToolResult(
                success=False,
                output="",
                error="Docker orchestrator not available for file operations",
            )
        
        try:
            # Use ls -la to list directory contents
            result = self.docker_orchestrator.execute_command(f"ls -la '{path}'")
            
            if result["exit_code"] == 0:
                content = result["output"]
                logger.debug(f"Successfully listed directory in container: {path}")
                
                return ToolResult(
                    success=True,
                    output=content,
                    metadata={"action": "list", "path": path},
                )
            else:
                error_output = result.get("error", "Unknown error")
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Failed to list directory: {error_output}",
                    metadata={"action": "list", "path": path},
                )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Container directory list failed: {str(e)}",
                metadata={"action": "list", "path": path},
            )

    def _read_file(self, file_path: Path) -> ToolResult:
        """Read a file."""
        if not file_path.exists():
            return ToolResult(success=False, output="", error=f"File does not exist: {file_path}")

        if not file_path.is_file():
            return ToolResult(success=False, output="", error=f"Path is not a file: {file_path}")

        try:
            content = file_path.read_text(encoding="utf-8")
            logger.debug(f"Successfully read file: {file_path} ({len(content)} chars)")

            return ToolResult(
                success=True,
                output=content,
                metadata={"action": "read", "path": str(file_path), "size": len(content)},
            )
        except UnicodeDecodeError:
            # Try reading as binary and show first few bytes
            try:
                raw_content = file_path.read_bytes()
                preview = raw_content[:100].hex()
                return ToolResult(
                    success=False,
                    output="",
                    error=f"File appears to be binary. First 100 bytes (hex): {preview}",
                    metadata={"action": "read", "path": str(file_path), "binary": True},
                )
            except Exception as e:
                return ToolResult(success=False, output="", error=f"Failed to read file: {str(e)}")

    def _write_file(self, file_path: Path, content: str) -> ToolResult:
        """Write content to a file."""
        try:
            # Create parent directories if they don't exist
            file_path.parent.mkdir(parents=True, exist_ok=True)

            file_path.write_text(content, encoding="utf-8")
            logger.debug(f"Successfully wrote file: {file_path} ({len(content)} chars)")

            return ToolResult(
                success=True,
                output=f"Successfully wrote {len(content)} characters to {file_path}",
                metadata={"action": "write", "path": str(file_path), "size": len(content)},
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Failed to write file: {str(e)}")

    def _append_file(self, file_path: Path, content: str) -> ToolResult:
        """Append content to a file."""
        try:
            # Create parent directories if they don't exist
            file_path.parent.mkdir(parents=True, exist_ok=True)

            # Create file if it doesn't exist
            if not file_path.exists():
                file_path.touch()

            with file_path.open("a", encoding="utf-8") as f:
                f.write(content)

            logger.debug(f"Successfully appended to file: {file_path} ({len(content)} chars)")

            return ToolResult(
                success=True,
                output=f"Successfully appended {len(content)} characters to {file_path}",
                metadata={"action": "append", "path": str(file_path), "size": len(content)},
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Failed to append to file: {str(e)}")

    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool."""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["read", "write", "append"],
                    "description": "The file operation to perform",
                },
                "path": {
                    "type": "string",
                    "description": "The file path (relative to /workspace or absolute)",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write or append (not needed for read)",
                    "default": "",
                },
            },
            "required": ["action", "path"],
        }

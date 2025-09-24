"""
Full Output Storage Manager

This module handles storage and retrieval of full tool outputs that are truncated
in the main context files. It provides indexing and search capabilities.
"""

import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List
from loguru import logger
import re


class OutputStorageManager:
    """Manages storage of full outputs with indexing for efficient retrieval."""
    
    def __init__(self, storage_dir: Path, orchestrator=None):
        """
        Initialize the output storage manager.
        
        Args:
            storage_dir: Directory to store full outputs (usually .setup_agent/contexts/)
            orchestrator: Docker orchestrator for container file operations
        """
        self.storage_dir = Path(storage_dir)
        self.orchestrator = orchestrator
        
        # If we have an orchestrator, ensure directory exists in container
        if self.orchestrator:
            try:
                # Create directory in container
                mkdir_result = self.orchestrator.execute_command(f"mkdir -p {self.storage_dir}")
                if mkdir_result.get("exit_code") == 0:
                    logger.debug(f"Output storage directory ensured in container: {self.storage_dir}")
                else:
                    logger.warning(f"Failed to create directory in container: {mkdir_result.get('output')}")
            except Exception as e:
                logger.error(f"Failed to create storage directory in container {self.storage_dir}: {e}")
        else:
            # Fallback to local filesystem (for testing)
            try:
                self.storage_dir.mkdir(parents=True, exist_ok=True)
                logger.debug(f"Output storage directory ensured locally: {self.storage_dir}")
            except (OSError, PermissionError) as e:
                # If we can't create the directory (e.g., /workspace doesn't exist), use temp dir
                import tempfile
                temp_dir = Path(tempfile.gettempdir()) / "sag_output_storage" / "contexts"
                temp_dir.mkdir(parents=True, exist_ok=True)
                self.storage_dir = temp_dir
                logger.warning(f"Could not create storage dir at {storage_dir}, using {self.storage_dir}: {e}")
                # Update file paths after changing storage_dir
                self.storage_file = self.storage_dir / "full_outputs.jsonl"
                self.index_file = self.storage_dir / "output_index.json"
            except Exception as e:
                logger.error(f"Failed to create storage directory {self.storage_dir}: {e}")

        # Set file paths (may have been updated in exception handler)
        if not hasattr(self, 'storage_file'):
            self.storage_file = self.storage_dir / "full_outputs.jsonl"
            self.index_file = self.storage_dir / "output_index.json"
        
        # Log initialization
        logger.info(f"OutputStorageManager initialized: storage_file={self.storage_file}, index_file={self.index_file}")
        
        self.current_index = self._load_index()
        
    def _load_index(self) -> Dict[str, Dict[str, Any]]:
        """Load the existing index or create a new one."""
        if self.orchestrator:
            # Check if index exists in container
            check_cmd = f"test -f {self.index_file} && cat {self.index_file}"
            check_result = self.orchestrator.execute_command(check_cmd)
            
            if check_result.get("exit_code") == 0 and check_result.get("output"):
                try:
                    return json.loads(check_result["output"])
                except Exception as e:
                    logger.warning(f"Failed to parse output index from container: {e}")
        else:
            # Local filesystem fallback
            if self.index_file.exists():
                try:
                    with open(self.index_file, 'r') as f:
                        return json.load(f)
                except Exception as e:
                    logger.warning(f"Failed to load output index: {e}")
        return {}
    
    def _save_index(self):
        """Save the current index to disk."""
        try:
            if self.orchestrator:
                # Save to container
                index_json = json.dumps(self.current_index, indent=2).replace('"', '\\"').replace('$', '\\$')
                save_cmd = f'echo "{index_json}" > {self.index_file}'
                save_result = self.orchestrator.execute_command(save_cmd)
                
                if save_result.get("exit_code") != 0:
                    logger.error(f"Failed to save index to container: {save_result.get('output')}")
            else:
                # Local filesystem fallback
                with open(self.index_file, 'w') as f:
                    json.dump(self.current_index, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save output index: {e}")
    
    def store_output(
        self,
        task_id: str,
        tool_name: str,
        output: str,
        timestamp: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Store a full output and return its reference ID.
        
        Args:
            task_id: The task ID this output belongs to
            tool_name: Name of the tool that generated this output
            output: The full output text
            timestamp: Optional timestamp (defaults to now)
            metadata: Optional additional metadata
            
        Returns:
            Reference ID for retrieving this output
        """
        # Generate a unique ID for this output
        timestamp = timestamp or datetime.now().isoformat()
        content_hash = hashlib.md5(f"{task_id}_{tool_name}_{timestamp}_{output[:100]}".encode()).hexdigest()[:12]
        ref_id = f"output_{content_hash}"
        
        # Create the output record
        record = {
            "ref_id": ref_id,
            "task_id": task_id,
            "tool_name": tool_name,
            "timestamp": timestamp,
            "output_length": len(output),
            "output": output,
            "metadata": metadata or {}
        }
        
        # Append to storage file (JSONL format for efficient appending)
        try:
            # Store in container if orchestrator is available
            if self.orchestrator:
                # Write to container using echo command
                json_line = json.dumps(record).replace('"', '\\"').replace('$', '\\$')
                write_cmd = f'echo "{json_line}" >> {self.storage_file}'
                write_result = self.orchestrator.execute_command(write_cmd)
                
                if write_result.get("exit_code") == 0:
                    logger.debug(f"Stored output in container: ref_id={ref_id}, task={task_id}, tool={tool_name}, length={len(output)}")
                else:
                    logger.error(f"Failed to write to container file: {write_result.get('output')}")
                    return ""
            else:
                # Fallback to local filesystem (for testing)
                # Ensure file exists before appending
                if not self.storage_file.exists():
                    self.storage_file.touch()
                    logger.debug(f"Created storage file locally: {self.storage_file}")
                
                with open(self.storage_file, 'a') as f:
                    f.write(json.dumps(record) + '\n')
                    logger.debug(f"Stored output locally: ref_id={ref_id}, task={task_id}, tool={tool_name}, length={len(output)}")
        except Exception as e:
            logger.error(f"Failed to store output to {self.storage_file}: {e}")
            return ""
        
        # Update index with searchable metadata
        self.current_index[ref_id] = {
            "task_id": task_id,
            "tool_name": tool_name,
            "timestamp": timestamp,
            "output_length": len(output),
            "line_number": self._count_lines_in_file(),  # Line number in JSONL file
            "first_100_chars": output[:100],
            "last_100_chars": output[-100:] if len(output) > 100 else output,
            "metadata": metadata or {}
        }
        
        self._save_index()
        logger.debug(f"Stored full output with ref_id: {ref_id} ({len(output)} chars)")
        return ref_id
    
    def _count_lines_in_file(self) -> int:
        """Count the number of lines in the storage file."""
        if self.orchestrator:
            # Count lines in container file
            count_cmd = f"test -f {self.storage_file} && wc -l < {self.storage_file} || echo 0"
            count_result = self.orchestrator.execute_command(count_cmd)
            
            if count_result.get("exit_code") == 0:
                try:
                    return int(count_result.get("output", "0").strip())
                except:
                    return 0
            return 0
        else:
            # Local filesystem fallback
            if not self.storage_file.exists():
                return 0
            try:
                with open(self.storage_file, 'r') as f:
                    return sum(1 for _ in f)
            except:
                return 0
    
    def retrieve_output(self, ref_id: str) -> Optional[str]:
        """
        Retrieve a full output by its reference ID.
        
        Args:
            ref_id: The reference ID returned by store_output
            
        Returns:
            The full output text, or None if not found
        """
        if ref_id not in self.current_index:
            logger.warning(f"Reference ID not found in index: {ref_id}")
            return None
        
        index_info = self.current_index[ref_id]
        line_number = index_info.get("line_number", 0)
        
        try:
            if self.orchestrator:
                # Retrieve from container
                retrieve_cmd = f"sed -n '{line_number}p' {self.storage_file}"
                retrieve_result = self.orchestrator.execute_command(retrieve_cmd)
                
                if retrieve_result.get("exit_code") == 0 and retrieve_result.get("output"):
                    record = json.loads(retrieve_result["output"])
                    return record.get("output", "")
            else:
                # Local filesystem fallback
                with open(self.storage_file, 'r') as f:
                    for i, line in enumerate(f, 1):
                        if i == line_number:
                            record = json.loads(line)
                            return record.get("output", "")
        except Exception as e:
            logger.error(f"Failed to retrieve output: {e}")
        
        return None
    
    def search_outputs(
        self,
        pattern: Optional[str] = None,
        task_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Search for outputs matching criteria.
        
        Args:
            pattern: Regex pattern to search in outputs
            task_id: Filter by task ID
            tool_name: Filter by tool name
            limit: Maximum number of results
            
        Returns:
            List of matching output references with snippets
        """
        results = []
        
        # First, filter by index criteria
        candidates = []
        for ref_id, info in self.current_index.items():
            if task_id and info.get("task_id") != task_id:
                continue
            if tool_name and info.get("tool_name") != tool_name:
                continue
            candidates.append((ref_id, info))
        
        # If pattern provided, search in full outputs
        if pattern:
            try:
                regex = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
            except re.error as e:
                logger.error(f"Invalid regex pattern: {e}")
                return []
            
            for ref_id, info in candidates:
                if len(results) >= limit:
                    break
                    
                # Retrieve and search full output
                output = self.retrieve_output(ref_id)
                if output:
                    matches = regex.findall(output)
                    if matches:
                        # Get context around first match
                        match_obj = regex.search(output)
                        if match_obj:
                            start = max(0, match_obj.start() - 100)
                            end = min(len(output), match_obj.end() + 100)
                            snippet = output[start:end]
                        else:
                            snippet = matches[0][:200] if matches else ""
                        
                        results.append({
                            "ref_id": ref_id,
                            "task_id": info["task_id"],
                            "tool_name": info["tool_name"],
                            "timestamp": info["timestamp"],
                            "match_count": len(matches),
                            "snippet": snippet,
                            "output_length": info["output_length"]
                        })
        else:
            # No pattern, just return metadata
            for ref_id, info in candidates[:limit]:
                results.append({
                    "ref_id": ref_id,
                    "task_id": info["task_id"],
                    "tool_name": info["tool_name"],
                    "timestamp": info["timestamp"],
                    "first_100": info["first_100_chars"],
                    "last_100": info["last_100_chars"],
                    "output_length": info["output_length"]
                })
        
        return results
    
    def get_truncation_with_reference(
        self,
        output: str,
        ref_id: str,
        max_length: int = 800,
        tool_name: Optional[str] = None
    ) -> str:
        """
        Create a truncated version of output with reference to full text.
        
        Args:
            output: The full output text
            ref_id: Reference ID for the full output
            max_length: Maximum length for truncated version
            tool_name: Optional tool name for context
            
        Returns:
            Truncated output with reference information
        """
        if len(output) <= max_length:
            return output
        
        # Calculate how much to show from beginning and end
        half_length = (max_length - 150) // 2  # Reserve 150 chars for reference message
        
        # Create reference message
        ref_msg = (
            f"\n... [Output truncated: showing {half_length} of {len(output)} chars] ...\n"
            f"... [Full output ref: {ref_id}] ...\n"
            f"... [Search with: grep pattern .setup_agent/contexts/full_outputs.jsonl] ...\n"
        )
        
        # Build truncated version
        truncated = output[:half_length] + ref_msg + output[-half_length:]
        
        return truncated
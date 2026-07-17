"""
Full Output Storage Manager

This module handles storage and retrieval of full tool outputs that are truncated
in the main context files. It provides indexing and search capabilities.
"""

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from sag.tools.base import (
    ToolResult,
    bind_tool_result_output_storage,
    is_output_storage_ref,
)
from sag.utils.container_io import write_container_text


class OutputDurabilityError(RuntimeError):
    """Raised when no validated durable home can be established for output."""


def _output_round_trips(storage: Any, ref: str, output: str) -> bool:
    if not is_output_storage_ref(ref):
        return False
    try:
        return storage.retrieve_output(ref) == output
    except Exception as exc:
        logger.warning(f"Failed to validate output reference {ref}: {exc}")
        return False


def _with_validated_output_ref(
    result: ToolResult,
    storage: Any,
    *,
    ref: str,
    task_id: str,
    tool_name: str,
) -> ToolResult:
    payload = result.model_dump(mode="python")
    payload["output_ref"] = ref
    with bind_tool_result_output_storage(
        storage,
        task_id=task_id,
        tool_name=tool_name,
    ):
        return ToolResult.model_validate(payload)


def attach_durable_output_ref(
    result: ToolResult,
    storage: "OutputStorageManager",
    *,
    task_id: str,
    tool_name: str,
) -> ToolResult:
    """Return a detached result whose full output has durable provenance."""
    output = (result.raw_output if result.raw_output is not None else result.output) or ""
    if result.output_ref:
        if _output_round_trips(storage, result.output_ref, output):
            return result
        logger.warning(
            f"Re-persisting inaccessible output reference {result.output_ref} for {tool_name}"
        )
    metadata = {
        "invocation_status": result.invocation_status.value,
        "operation_outcome": result.operation_outcome.value,
        "evidence_status": result.evidence_status.value,
        "error_code": result.error_code,
        "failure_signature": result.failure_signature,
    }
    failures: list[str] = []
    persistence_methods = (
        ("primary", getattr(storage, "store_output", None)),
        ("emergency", getattr(storage, "store_emergency_output", None)),
    )
    for label, persist in persistence_methods:
        if not callable(persist):
            failures.append(f"{label} persistence is unavailable")
            continue
        try:
            ref = persist(
                task_id=task_id,
                tool_name=tool_name,
                output=output,
                metadata=metadata,
            )
        except Exception as exc:
            failures.append(f"{label} persistence raised {type(exc).__name__}")
            continue
        if not is_output_storage_ref(ref):
            failures.append(f"{label} persistence returned no durable reference")
            continue
        if not _output_round_trips(storage, ref, output):
            failures.append(f"{label} tool output reference is not immediately retrievable")
            continue
        try:
            return _with_validated_output_ref(
                result,
                storage,
                ref=ref,
                task_id=task_id,
                tool_name=tool_name,
            )
        except (TypeError, ValueError) as exc:
            failures.append(f"{label} result validation raised {type(exc).__name__}")

    raise OutputDurabilityError(
        "primary and emergency output persistence failed: " + "; ".join(failures)
    )


def atomic_write_container_text(orchestrator, path: str, content: str) -> None:
    """Persist exact text with a temp write followed by an atomic rename."""
    tmp_path = f"{path}.tmp"
    if not write_container_text(orchestrator, tmp_path, content):
        raise OSError(f"failed to write temporary file {tmp_path}")

    # write_container_text appends one newline for JSONL callers. Remove only
    # that helper-owned byte so canonical JSON bytes and returned JSON match.
    trim_result = orchestrator.execute_command(f"truncate -s -1 {tmp_path}")
    if not (trim_result.get("exit_code") == 0 or trim_result.get("success")):
        raise OSError(f"failed to finalize temporary file {tmp_path}")
    rename_result = orchestrator.execute_command(f"mv {tmp_path} {path}")
    if not (rename_result.get("exit_code") == 0 or rename_result.get("success")):
        raise OSError(f"failed to atomically rename {tmp_path} to {path}")


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

        # Define container paths as strings for Docker commands
        # These are the actual paths inside the container, not host paths
        self.container_storage_dir = "/workspace/.setup_agent/contexts"
        self.container_storage_file = f"{self.container_storage_dir}/full_outputs.jsonl"
        self.container_index_file = f"{self.container_storage_dir}/output_index.json"

        # If we have an orchestrator, ensure directory exists in container
        if self.orchestrator:
            try:
                # Create directory in container using container path
                mkdir_result = self.orchestrator.execute_command(
                    f"mkdir -p {self.container_storage_dir}"
                )
                if mkdir_result.get("exit_code") == 0:
                    logger.debug(
                        f"Output storage directory ensured in container: {self.container_storage_dir}"
                    )
                else:
                    logger.warning(
                        f"Failed to create directory in container: {mkdir_result.get('output')}"
                    )
            except Exception as e:
                logger.error(
                    f"Failed to create storage directory in container {self.container_storage_dir}: {e}"
                )
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
                logger.warning(
                    f"Could not create storage dir at {storage_dir}, using {self.storage_dir}: {e}"
                )
                # Update file paths after changing storage_dir
                self.storage_file = self.storage_dir / "full_outputs.jsonl"
                self.index_file = self.storage_dir / "output_index.json"
            except Exception as e:
                logger.error(f"Failed to create storage directory {self.storage_dir}: {e}")

        # Set file paths for local operations (may have been updated in exception handler)
        if not hasattr(self, "storage_file"):
            self.storage_file = self.storage_dir / "full_outputs.jsonl"
            self.index_file = self.storage_dir / "output_index.json"

        # Log initialization - show both container and local paths when using orchestrator
        if self.orchestrator:
            logger.info(
                f"OutputStorageManager initialized with orchestrator: container_files={self.container_storage_file}, {self.container_index_file}"
            )
        else:
            logger.info(
                f"OutputStorageManager initialized locally: storage_file={self.storage_file}, index_file={self.index_file}"
            )

        self.current_index = self._load_index()

    def _load_index(self) -> Dict[str, Dict[str, Any]]:
        """Load the existing index or create a new one."""
        if self.orchestrator:
            # Check if index exists in container using container path
            check_cmd = f"test -f {self.container_index_file} && cat {self.container_index_file}"
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
                    with open(self.index_file, "r") as f:
                        return json.load(f)
                except Exception as e:
                    logger.warning(f"Failed to load output index: {e}")
        return self._rebuild_index_from_storage()

    def _save_index(self):
        """Save the current index to disk."""
        try:
            if self.orchestrator:
                index_json = json.dumps(self.current_index, indent=2)
                if not self._write_container_text(self.container_index_file, index_json):
                    raise OSError("failed to save output index to container")
            else:
                # Local filesystem fallback
                with open(self.index_file, "w") as f:
                    json.dump(self.current_index, f, indent=2)
        except Exception as exc:
            raise OSError(f"failed to save output index: {exc}") from exc

    def _write_container_text(self, path: str, content: str, *, append: bool = False) -> bool:
        # Delegate to the shared writer: it keeps the fast single-command heredoc
        # for small content and streams large content as base64 chunks, so a big
        # payload never trips the kernel per-arg limit ("argument list too long").
        return write_container_text(self.orchestrator, path, content, append=append)

    @staticmethod
    def _index_entry(record: Dict[str, Any], line_number: int) -> Dict[str, Any]:
        output = str(record.get("output") or "")
        return {
            "task_id": record.get("task_id"),
            "tool_name": record.get("tool_name"),
            "timestamp": record.get("timestamp"),
            "output_length": record.get("output_length", len(output)),
            "line_number": line_number,
            "first_100_chars": output[:100],
            "last_100_chars": output[-100:] if len(output) > 100 else output,
            "metadata": record.get("metadata") or {},
        }

    def _read_storage_line(self, line_number: int) -> Optional[Dict[str, Any]]:
        try:
            if self.orchestrator:
                result = self.orchestrator.execute_command(
                    f"sed -n '{line_number}p' {self.container_storage_file}"
                )
                if result.get("exit_code") != 0 or not result.get("output"):
                    return None
                return json.loads(result["output"])

            if not self.storage_file.exists():
                return None
            with open(self.storage_file, "r") as storage_file:
                for current, line in enumerate(storage_file, 1):
                    if current == line_number:
                        return json.loads(line)
        except Exception as exc:
            logger.warning(f"Failed to read output storage line {line_number}: {exc}")
        return None

    def _rebuild_index_from_storage(self) -> Dict[str, Dict[str, Any]]:
        """Recover searchable metadata from the append-only JSONL source of truth."""
        rebuilt: Dict[str, Dict[str, Any]] = {}
        for line_number in range(1, self._count_lines_in_file() + 1):
            record = self._read_storage_line(line_number)
            if not isinstance(record, dict):
                continue
            ref_id = record.get("ref_id")
            if not is_output_storage_ref(ref_id):
                continue
            rebuilt[ref_id] = self._index_entry(record, line_number)
        return rebuilt

    def _index_may_be_stale(self) -> bool:
        indexed_line = max(
            (
                int(info.get("line_number", 0) or 0)
                for info in self.current_index.values()
                if isinstance(info, dict)
            ),
            default=0,
        )
        return self._count_lines_in_file() > indexed_line

    def store_output(
        self,
        task_id: str,
        tool_name: str,
        output: str,
        timestamp: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
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
        content_hash = hashlib.md5(
            f"{task_id}_{tool_name}_{timestamp}_{output[:100]}".encode()
        ).hexdigest()[:12]
        ref_id = f"output_{content_hash}"

        # Create the output record
        record = {
            "ref_id": ref_id,
            "task_id": task_id,
            "tool_name": tool_name,
            "timestamp": timestamp,
            "output_length": len(output),
            "output": output,
            "metadata": metadata or {},
        }

        # Append to storage file (JSONL format for efficient appending)
        try:
            # Store in container if orchestrator is available
            if self.orchestrator:
                json_line = json.dumps(record)
                if self._write_container_text(self.container_storage_file, json_line, append=True):
                    logger.debug(
                        f"Stored output in container: ref_id={ref_id}, task={task_id}, tool={tool_name}, length={len(output)}"
                    )
                else:
                    return ""
            else:
                # Fallback to local filesystem (for testing)
                # Ensure file exists before appending
                if not self.storage_file.exists():
                    self.storage_file.touch()
                    logger.debug(f"Created storage file locally: {self.storage_file}")

                with open(self.storage_file, "a") as f:
                    f.write(json.dumps(record) + "\n")
                    logger.debug(
                        f"Stored output locally: ref_id={ref_id}, task={task_id}, tool={tool_name}, length={len(output)}"
                    )
        except Exception as e:
            logger.error(f"Failed to store output to {self.storage_file}: {e}")
            return ""

        # Reload-and-merge before saving: _save_index() overwrites the shared
        # container index file from this instance's in-memory copy. Other
        # OutputStorageManager instances (each tool builds its own) append to the
        # SAME jsonl/index, so saving a stale cache would clobber their refs — e.g.
        # the build tool's manager wiping the maven compile-log ref, after which the
        # agent's output_search returns "No output found" and it cannot diagnose the
        # build. Refresh from disk so we add to the union, never overwrite it. The
        # jsonl is global/append-only, so the line_number below stays valid.
        self.current_index = self._load_index()

        # Update index with searchable metadata
        self.current_index[ref_id] = {
            "task_id": task_id,
            "tool_name": tool_name,
            "timestamp": timestamp,
            "output_length": len(output),
            "line_number": self._count_lines_in_file(),  # Line number in JSONL file
            "first_100_chars": output[:100],
            "last_100_chars": output[-100:] if len(output) > 100 else output,
            "metadata": metadata or {},
        }

        try:
            self._save_index()
        except OSError as exc:
            recovered = self._rebuild_index_from_storage()
            if ref_id not in recovered:
                raise OSError(
                    f"output index persistence failed and JSONL recovery lost {ref_id}"
                ) from exc
            self.current_index = recovered
            logger.warning(f"Output index write failed; using JSONL recovery: {exc}")
        logger.debug(f"Stored full output with ref_id: {ref_id} ({len(output)} chars)")
        return ref_id

    def _emergency_path(self, ref_id: str) -> Path:
        return self.storage_dir / f"emergency-{ref_id}.json"

    def _container_emergency_path(self, ref_id: str) -> str:
        return f"{self.container_storage_dir}/emergency-{ref_id}.json"

    def _read_emergency_record(self, ref_id: str) -> Optional[Dict[str, Any]]:
        if not ref_id.startswith("output_emergency_") or not is_output_storage_ref(ref_id):
            return None
        try:
            if self.orchestrator:
                path = self._container_emergency_path(ref_id)
                result = self.orchestrator.execute_command(f"test -f {path} && cat {path}")
                if result.get("exit_code") != 0 or not result.get("output"):
                    return None
                record = json.loads(result["output"])
            else:
                path = self._emergency_path(ref_id)
                if not path.is_file():
                    return None
                record = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"Failed to read emergency output record {ref_id}: {exc}")
            return None
        if not isinstance(record, dict) or record.get("ref_id") != ref_id:
            return None
        return record

    def store_emergency_output(
        self,
        task_id: str,
        tool_name: str,
        output: str,
        timestamp: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Store one content-addressed record outside the primary JSONL/index pair."""
        identity = {
            "task_id": task_id,
            "tool_name": tool_name,
            "timestamp": timestamp,
            "output_length": len(output),
            "output": output,
            "metadata": metadata or {},
            "storage_mode": "emergency",
        }
        canonical_identity = json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        )
        digest = hashlib.sha256(canonical_identity.encode("utf-8")).hexdigest()[:24]
        ref_id = f"output_emergency_{digest}"
        record_json = json.dumps(
            {"ref_id": ref_id, **identity},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        )

        try:
            if self.orchestrator:
                atomic_write_container_text(
                    self.orchestrator,
                    self._container_emergency_path(ref_id),
                    record_json,
                )
            else:
                path = self._emergency_path(ref_id)
                temp_path = path.with_suffix(f"{path.suffix}.tmp")
                temp_path.write_text(record_json, encoding="utf-8")
                temp_path.replace(path)
        except Exception as exc:
            raise OSError(f"failed to store emergency output {ref_id}: {exc}") from exc

        logger.warning(f"Stored output in emergency record: {ref_id}")
        return ref_id

    def _count_lines_in_file(self) -> int:
        """Count the number of lines in the storage file."""
        if self.orchestrator:
            # Count lines in container file using container path
            count_cmd = f"test -f {self.container_storage_file} && wc -l < {self.container_storage_file} || echo 0"
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
                with open(self.storage_file, "r") as f:
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
        emergency_record = self._read_emergency_record(ref_id)
        if emergency_record is not None:
            return str(emergency_record.get("output") or "")

        if ref_id not in self.current_index:
            # current_index is an in-memory cache populated at construction time.
            # Another OutputStorageManager instance (e.g. the build tool's own
            # manager) may have stored this output AFTER we loaded our copy, so a
            # miss here is often just a stale cache rather than a real absence.
            # Reload the durable container index before giving up — this is what
            # keeps detached build logs retrievable across separate tool instances
            # (the OutputSearchTool builds its own manager once per session).
            self.current_index = self._load_index()

        if ref_id not in self.current_index:
            if self._index_may_be_stale():
                self.current_index = self._rebuild_index_from_storage()
            if ref_id not in self.current_index:
                logger.warning(f"Reference ID not found in index or JSONL: {ref_id}")
                return None

        index_info = self.current_index[ref_id]
        line_number = index_info.get("line_number", 0)

        try:
            if self.orchestrator:
                # Retrieve from container using container path
                record = self._read_storage_line(line_number)
                if record and record.get("ref_id") == ref_id:
                    return record.get("output", "")
            else:
                # Local filesystem fallback
                with open(self.storage_file, "r") as f:
                    for i, line in enumerate(f, 1):
                        if i == line_number:
                            record = json.loads(line)
                            return record.get("output", "")
        except Exception as e:
            logger.error(f"Failed to retrieve output: {e}")

        return None

    def has_output_ref(self, ref_id: str) -> bool:
        """Check the primary index or deterministic emergency record."""
        if self._read_emergency_record(ref_id) is not None:
            return True
        self.current_index = self._load_index()
        if ref_id not in self.current_index and self._index_may_be_stale():
            self.current_index = self._rebuild_index_from_storage()
        return ref_id in self.current_index

    def search_outputs(
        self,
        pattern: Optional[str] = None,
        task_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        limit: int = 10,
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

        # Refresh from the durable index first: another manager instance may have
        # stored outputs since we loaded our in-memory copy at construction (see
        # retrieve_output). Without this, search/list miss build outputs written by
        # the build tool's separate manager.
        self.current_index = self._load_index()
        if self._index_may_be_stale():
            self.current_index = self._rebuild_index_from_storage()

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

                        results.append(
                            {
                                "ref_id": ref_id,
                                "task_id": info["task_id"],
                                "tool_name": info["tool_name"],
                                "timestamp": info["timestamp"],
                                "match_count": len(matches),
                                "snippet": snippet,
                                "output_length": info["output_length"],
                            }
                        )
        else:
            # No pattern, just return metadata
            for ref_id, info in candidates[:limit]:
                results.append(
                    {
                        "ref_id": ref_id,
                        "task_id": info["task_id"],
                        "tool_name": info["tool_name"],
                        "timestamp": info["timestamp"],
                        "first_100": info["first_100_chars"],
                        "last_100": info["last_100_chars"],
                        "output_length": info["output_length"],
                    }
                )

        return results

    def get_truncation_with_reference(
        self, output: str, ref_id: str, max_length: int = 800, tool_name: Optional[str] = None
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

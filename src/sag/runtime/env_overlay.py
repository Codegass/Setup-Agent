"""Agent-maintained runtime environment overlay persistence."""

from __future__ import annotations

import base64
import json
import posixpath
import re
import shlex
from copy import deepcopy
from typing import Any, Optional

DEFAULT_OVERLAY_JSON = "/workspace/.setup_agent/env_overlay.json"
DEFAULT_OVERLAY_SCRIPT = "/workspace/.setup_agent/env_overlay.sh"

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOOL_ALIASES = {"mvn": "maven"}


class EnvOverlayWarning(UserWarning):
    """Warning marker for recoverable overlay state problems."""


class EnvOverlayStore:
    """Persist runtime tool environment overlays inside the workspace."""

    def __init__(
        self,
        orchestrator: Any,
        *,
        overlay_json: str = DEFAULT_OVERLAY_JSON,
        overlay_script: str = DEFAULT_OVERLAY_SCRIPT,
    ):
        self.orchestrator = orchestrator
        self.overlay_json = overlay_json
        self.overlay_script = overlay_script

    def inspect(self) -> dict[str, Any]:
        """Return the current overlay, recovering invalid state to an empty overlay."""
        overlay, warnings = self._load_overlay()
        result = deepcopy(overlay)
        if warnings:
            result["warnings"] = warnings
        return result

    def register(
        self,
        tool: str,
        executable: str,
        *,
        version: Optional[str] = None,
        source: str = "agent_registered",
        env: Optional[dict[str, Any]] = None,
        path_prepend: Optional[list[str] | str] = None,
        activate: bool = False,
    ) -> dict[str, Any]:
        """Register a candidate executable and optionally make it active."""
        overlay, _warnings = self._load_overlay()
        tool_name = self._normalize_tool(tool)
        executable_path = self._normalize_tool_executable(tool_name, executable)
        entry = self._tool_entry(overlay, tool_name)
        candidates = entry.setdefault("candidates", {})
        existing = candidates.get(executable_path, {})

        normalized_env = self._normalize_env(env if env is not None else existing.get("env", {}))
        normalized_path = self._normalize_path_prepend(
            path_prepend if path_prepend is not None else existing.get("path_prepend"),
            executable_path,
        )

        candidates[executable_path] = {
            "version": str(version) if version is not None else existing.get("version"),
            "source": source,
            "env": normalized_env,
            "path_prepend": normalized_path,
        }

        entry.setdefault("blocked", [])
        if activate:
            self._activate_in_overlay(overlay, tool_name, executable_path)
        return self._write_overlay(overlay)

    def activate(self, tool: str, executable: str) -> dict[str, Any]:
        """Activate a registered executable for a tool."""
        overlay, _warnings = self._load_overlay()
        tool_name = self._normalize_tool(tool)
        executable_path = self._normalize_tool_executable(tool_name, executable)
        self._activate_in_overlay(overlay, tool_name, executable_path)
        return self._write_overlay(overlay)

    def block(
        self,
        tool: str,
        executable: str,
        *,
        version: Optional[str] = None,
        requirement: Optional[str] = None,
        reason: Optional[str] = None,
        source: str = "build_error",
    ) -> dict[str, Any]:
        """Record negative evidence for one exact executable."""
        overlay, _warnings = self._load_overlay()
        tool_name = self._normalize_tool(tool)
        executable_path = self._normalize_tool_executable(tool_name, executable)
        entry = self._tool_entry(overlay, tool_name)
        block_record = {
            "executable": executable_path,
            "version": str(version) if version is not None else None,
            "requirement": requirement,
            "reason": reason,
            "source": source,
        }

        blocked = entry.setdefault("blocked", [])
        blocked[:] = [
            item
            for item in blocked
            if not (
                item.get("executable") == executable_path
                and item.get("version") == block_record["version"]
                and item.get("requirement") == requirement
            )
        ]
        blocked.append(block_record)

        if entry.get("active") == executable_path:
            entry.pop("active", None)

        return self._write_overlay(overlay)

    def record_requirement_failure(
        self,
        tool: str,
        *,
        requirement: str,
        executable: Optional[str] = None,
        version: Optional[str] = None,
        reason: Optional[str] = None,
        source: str = "build_error",
        working_directory: Optional[str] = None,
    ) -> dict[str, Any]:
        """Atomically persist an observed constraint and exact negative evidence."""
        overlay, _warnings = self._load_overlay()
        tool_name = self._normalize_tool(tool)
        raw_requirement = str(requirement or "").strip()
        if not raw_requirement:
            raise ValueError("requirement is required")

        entry = self._tool_entry(overlay, tool_name)
        requirement_record = {
            "raw": raw_requirement,
            "source": str(source or "build_error"),
            "working_directory": (
                str(working_directory).rstrip("/") if working_directory else None
            ),
        }
        requirements = entry.setdefault("requirements", [])
        if not any(
            item.get("raw") == requirement_record["raw"]
            and item.get("working_directory") == requirement_record["working_directory"]
            for item in requirements
        ):
            requirements.append(requirement_record)

        if executable:
            executable_path = self._normalize_tool_executable(tool_name, executable)
            block_record = {
                "executable": executable_path,
                "version": str(version) if version is not None else None,
                "requirement": raw_requirement,
                "reason": reason,
                "source": str(source or "build_error"),
            }
            blocked = entry.setdefault("blocked", [])
            blocked[:] = [
                item
                for item in blocked
                if not (
                    item.get("executable") == executable_path
                    and item.get("version") == block_record["version"]
                    and item.get("requirement") == raw_requirement
                )
            ]
            blocked.append(block_record)
            if entry.get("active") == executable_path:
                entry.pop("active", None)

        return self._write_overlay(overlay)

    def observed_requirements(
        self,
        tool: str,
        *,
        working_directory: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Return applicable harness-observed constraints without weakening history."""
        overlay, _warnings = self._load_overlay()
        entry = overlay.get("tools", {}).get(self._normalize_tool(tool), {})
        records = entry.get("requirements", [])
        return [
            deepcopy(record)
            for record in records
            if self._requirement_applies(record, working_directory)
        ]

    def observed_requirement(
        self,
        tool: str,
        *,
        working_directory: Optional[str] = None,
    ) -> dict[str, Any] | None:
        """Backward-compatible first applicable harness-observed constraint."""
        requirements = self.observed_requirements(
            tool,
            working_directory=working_directory,
        )
        return requirements[0] if requirements else None

    def clear(self, tool: Optional[str] = None) -> dict[str, Any]:
        """Clear the whole overlay or one tool entry."""
        overlay, _warnings = self._load_overlay()
        if tool is None:
            overlay = self._empty_overlay()
        else:
            overlay.setdefault("tools", {}).pop(self._normalize_tool(tool), None)
        return self._write_overlay(overlay)

    def active_candidate(
        self,
        tool: str,
        *,
        overlay: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any] | None:
        """Return the active candidate metadata for a tool, including its executable."""
        if overlay is None:
            overlay, _warnings = self._load_overlay()
        tool_name = self._normalize_tool(tool)
        entry = overlay.get("tools", {}).get(tool_name, {})
        active = entry.get("active")
        if not active:
            return None
        candidate = entry.get("candidates", {}).get(active)
        if not candidate:
            return None
        return {"executable": active, **deepcopy(candidate)}

    def is_blocked(
        self,
        tool: str,
        executable: str,
        version: Optional[str] = None,
        requirement: Optional[str] = None,
    ) -> bool:
        """Return whether the exact executable has matching negative evidence."""
        overlay, _warnings = self._load_overlay()
        return self._is_blocked_in_overlay(
            overlay,
            self._normalize_tool(tool),
            self._normalize_tool_executable(self._normalize_tool(tool), executable),
            version=version,
            requirement=requirement,
        )

    def _load_overlay(self) -> tuple[dict[str, Any], list[str]]:
        raw = self._read_file(self.overlay_json)
        if raw is None or not raw.strip():
            return self._empty_overlay(), []

        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            return self._empty_overlay(), [
                f"Ignored invalid env overlay JSON at {self.overlay_json}: {exc.msg}"
            ]

        if not isinstance(loaded, dict):
            return self._empty_overlay(), [
                f"Ignored invalid env overlay JSON at {self.overlay_json}: expected object"
            ]

        warnings: list[str] = []
        try:
            normalized = self._normalize_overlay(loaded, tolerant=True, warnings=warnings)
        except ValueError as exc:
            return self._empty_overlay(), [
                f"Ignored invalid env overlay data at {self.overlay_json}: {exc}"
            ]
        return normalized, warnings

    def _write_overlay(self, overlay: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_overlay(overlay)
        payload = json.dumps(normalized, indent=2, sort_keys=True)
        script = self._render_shell_script(normalized)

        self._ensure_overlay_dir()
        previous = {
            self.overlay_script: self._read_file(self.overlay_script),
            self.overlay_json: self._read_file(self.overlay_json),
        }

        # The shell overlay is the execution consumer and JSON is the
        # resolver/report consumer.  Publish shell first and JSON last as the
        # commit point, verify both readbacks, and restore the complete prior
        # pair on any write/readback failure.  A failed API call therefore
        # cannot leave future commands and tool resolution observing different
        # active runtimes.
        try:
            self._write_file(self.overlay_script, script)
            self._write_file(self.overlay_json, payload)
            self._verify_persisted_pair(payload=payload, script=script)
        except Exception as exc:
            rollback_errors = self._restore_persisted_pair(previous)
            if rollback_errors:
                raise RuntimeError(
                    "Env overlay transaction failed and rollback was incomplete: "
                    f"{exc}; rollback errors: {'; '.join(rollback_errors)}"
                ) from exc
            raise RuntimeError(
                f"Env overlay transaction failed; prior JSON/shell pair restored: {exc}"
            ) from exc
        return deepcopy(normalized)

    def _verify_persisted_pair(self, *, payload: str, script: str) -> None:
        observed_script = self._read_file(self.overlay_script)
        observed_json = self._read_file(self.overlay_json)
        if observed_script != script or observed_json != payload:
            raise RuntimeError("Env overlay JSON/shell readback did not match the pending commit")

    def _restore_persisted_pair(self, previous: dict[str, Optional[str]]) -> list[str]:
        errors: list[str] = []
        # Restore JSON first so the resolver returns to the old committed
        # state before restoring the execution overlay.
        for path in (self.overlay_json, self.overlay_script):
            try:
                old_content = previous[path]
                if old_content is None:
                    self._remove_file(path)
                else:
                    self._write_file(path, old_content)
            except Exception as rollback_exc:
                errors.append(f"{path}: {rollback_exc}")
        return errors

    def _render_shell_script(self, overlay: dict[str, Any]) -> str:
        lines = ["# Generated by Setup-Agent env overlay."]
        path_entries: list[str] = []
        seen_paths: set[str] = set()
        active_candidates: list[tuple[str, dict[str, Any]]] = []

        # Maven is the public weak-model recovery surface.  Its exact
        # executable directory must win even when another active tool or one of
        # its caller-supplied prefixes would sort ahead of it.
        tool_names = sorted(
            overlay.get("tools", {}),
            key=lambda name: (name != "maven", name),
        )
        for tool_name in tool_names:
            tool_entry = overlay["tools"][tool_name]
            active = tool_entry.get("active")
            if not active:
                continue
            candidate = tool_entry.get("candidates", {}).get(active)
            if not candidate:
                continue
            active_candidates.append((active, candidate))

            for key, value in sorted(candidate.get("env", {}).items()):
                lines.append(f"export {key}={shlex.quote(str(value))}")

        # Exact executable directories precede every optional caller prefix.
        # With Maven ordered first above, bare ``mvn`` and BuildTool's exact
        # resolver observe the same active runtime across working directories.
        for active, _candidate in active_candidates:
            directory = posixpath.dirname(active)
            if directory and directory != "." and directory not in seen_paths:
                seen_paths.add(directory)
                path_entries.append(directory)

        for _active, candidate in active_candidates:
            for path in candidate.get("path_prepend", []):
                if path not in seen_paths:
                    seen_paths.add(path)
                    path_entries.append(path)

        if path_entries:
            quoted_prefix = ":".join(shlex.quote(path) for path in path_entries)
            lines.append(f"export PATH={quoted_prefix}:$PATH")

        return "\n".join(lines) + "\n"

    def _read_file(self, path: str) -> Optional[str]:
        if hasattr(self.orchestrator, "read_file"):
            result = self.orchestrator.read_file(path)
            if isinstance(result, dict):
                if result.get("exit_code", 0) != 0 or result.get("success") is False:
                    return None
                return result.get("content") or result.get("output") or ""
            return str(result)

        files = getattr(self.orchestrator, "files", None)
        if isinstance(files, dict):
            return files.get(path)

        # Preserve the distinction between a missing file and a present empty
        # file.  Rollback needs that distinction on the first transaction:
        # treating ENOENT as "" would "restore" two corrupt empty overlay
        # files after a failed initial publish.
        result = self.orchestrator.execute_command(f"cat {shlex.quote(path)}")
        if result.get("exit_code", 0) != 0 or result.get("success") is False:
            return None
        return result.get("output", "")

    def _write_file(self, path: str, content: str) -> None:
        if hasattr(self.orchestrator, "write_file"):
            result = self.orchestrator.write_file(path, content)
            if isinstance(result, dict) and (
                result.get("success") is False or result.get("exit_code", 0) != 0
            ):
                raise RuntimeError(f"Failed to write {path}: {result.get('output', '')}")
            return

        payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
        command = f"printf %s {shlex.quote(payload)} | base64 -d > {shlex.quote(path)}"
        result = self.orchestrator.execute_command(command)
        if result.get("exit_code", 0) != 0 or result.get("success") is False:
            raise RuntimeError(f"Failed to write {path}: {result.get('output', '')}")

    def _remove_file(self, path: str) -> None:
        files = getattr(self.orchestrator, "files", None)
        if isinstance(files, dict):
            files.pop(path, None)
            return
        result = self.orchestrator.execute_command(f"rm -f {shlex.quote(path)}")
        if result.get("exit_code", 0) != 0 or result.get("success") is False:
            raise RuntimeError(f"Failed to remove {path}: {result.get('output', '')}")

    def _ensure_overlay_dir(self) -> None:
        directory = posixpath.dirname(self.overlay_json)
        result = self.orchestrator.execute_command(f"mkdir -p {shlex.quote(directory)}")
        if result.get("exit_code", 0) != 0 or result.get("success") is False:
            raise RuntimeError(f"Failed to create {directory}: {result.get('output', '')}")

    def _normalize_overlay(
        self,
        overlay: dict[str, Any],
        *,
        tolerant: bool = False,
        warnings: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        normalized = self._empty_overlay()
        canonical_tool_entries: dict[str, bool] = {}
        tools = overlay.get("tools", {})
        if not isinstance(tools, dict):
            return normalized

        for raw_tool_name, raw_entry in tools.items():
            if not isinstance(raw_tool_name, str) or not isinstance(raw_entry, dict):
                continue
            try:
                tool_name = self._normalize_tool(raw_tool_name)
            except ValueError as exc:
                if tolerant:
                    self._append_warning(
                        warnings,
                        f"Ignored invalid env overlay tool entry: {exc}",
                    )
                    continue
                raise
            entry: dict[str, Any] = {"candidates": {}, "blocked": []}

            candidates = raw_entry.get("candidates", {})
            if isinstance(candidates, dict):
                for raw_executable, raw_candidate in candidates.items():
                    try:
                        if not isinstance(raw_executable, str) or not raw_executable.strip():
                            continue
                        candidate = raw_candidate if isinstance(raw_candidate, dict) else {}
                        executable = self._normalize_tool_executable(
                            tool_name,
                            raw_executable,
                        )
                        raw_env = candidate.get("env") if candidate.get("env") is not None else {}
                        entry["candidates"][executable] = {
                            "version": (
                                str(candidate.get("version"))
                                if candidate.get("version") is not None
                                else None
                            ),
                            "source": str(candidate.get("source") or "agent_registered"),
                            "env": self._normalize_env(raw_env),
                            "path_prepend": self._normalize_path_prepend(
                                candidate.get("path_prepend"),
                                executable,
                            ),
                        }
                    except ValueError as exc:
                        if tolerant:
                            self._append_warning(
                                warnings,
                                (
                                    "Ignored invalid env overlay candidate for "
                                    f"{tool_name}: {exc}"
                                ),
                            )
                            continue
                        raise

            active = raw_entry.get("active")
            if isinstance(active, str) and active in entry["candidates"]:
                entry["active"] = active

            blocked = raw_entry.get("blocked", [])
            if isinstance(blocked, list):
                for raw_block in blocked:
                    if not isinstance(raw_block, dict) or not raw_block.get("executable"):
                        continue
                    try:
                        entry["blocked"].append(
                            {
                                "executable": self._normalize_tool_executable(
                                    tool_name,
                                    raw_block["executable"],
                                ),
                                "version": (
                                    str(raw_block.get("version"))
                                    if raw_block.get("version") is not None
                                    else None
                                ),
                                "requirement": raw_block.get("requirement"),
                                "reason": raw_block.get("reason"),
                                "source": str(raw_block.get("source") or "build_error"),
                            }
                        )
                    except ValueError as exc:
                        if tolerant:
                            self._append_warning(
                                warnings,
                                f"Ignored invalid env overlay block for {tool_name}: {exc}",
                            )
                            continue
                        raise

            raw_requirements = raw_entry.get("requirements")
            if not isinstance(raw_requirements, list):
                legacy_requirement = raw_entry.get("requirement")
                raw_requirements = (
                    [legacy_requirement] if isinstance(legacy_requirement, dict) else []
                )
            normalized_requirements = []
            for raw_requirement in raw_requirements:
                if not isinstance(raw_requirement, dict):
                    continue
                requirement_value = raw_requirement.get("raw")
                if not isinstance(requirement_value, str) or not requirement_value.strip():
                    continue
                normalized_record = {
                    "raw": requirement_value.strip(),
                    "source": str(raw_requirement.get("source") or "build_error"),
                    "working_directory": (
                        str(raw_requirement["working_directory"]).rstrip("/")
                        if raw_requirement.get("working_directory")
                        else None
                    ),
                }
                if normalized_record not in normalized_requirements:
                    normalized_requirements.append(normalized_record)
            if normalized_requirements:
                entry["requirements"] = normalized_requirements

            incoming_is_canonical = raw_tool_name.strip().lower() == tool_name
            existing = normalized["tools"].get(tool_name)
            if existing is None:
                normalized["tools"][tool_name] = entry
                canonical_tool_entries[tool_name] = incoming_is_canonical
                continue

            existing_is_canonical = canonical_tool_entries.get(tool_name, False)
            for executable, candidate in entry["candidates"].items():
                if executable not in existing["candidates"] or (
                    incoming_is_canonical and not existing_is_canonical
                ):
                    existing["candidates"][executable] = candidate

            for block in entry.get("blocked", []):
                if block not in existing["blocked"]:
                    existing["blocked"].append(block)

            existing_requirements = existing.setdefault("requirements", [])
            for requirement in entry.get("requirements", []):
                if requirement not in existing_requirements:
                    existing_requirements.append(requirement)
            if not existing_requirements:
                existing.pop("requirements", None)

            # A persisted canonical key is authoritative over the legacy
            # ``mvn`` alias, including an intentional lack of activation.  An
            # alias-only v1 overlay remains readable and is normalized to one
            # ``maven`` key without inventing a second active candidate.
            if incoming_is_canonical:
                if "active" in entry:
                    existing["active"] = entry["active"]
                else:
                    existing.pop("active", None)
            elif not existing_is_canonical and "active" in entry:
                existing["active"] = entry["active"]
            canonical_tool_entries[tool_name] = existing_is_canonical or incoming_is_canonical

        return normalized

    def _append_warning(self, warnings: Optional[list[str]], message: str) -> None:
        if warnings is not None:
            warnings.append(message)

    def _requirement_applies(
        self,
        record: dict[str, Any],
        working_directory: Optional[str],
    ) -> bool:
        if working_directory is None:
            return True
        raw_scope = str(record.get("working_directory") or "").strip()
        raw_requested = str(working_directory or "").strip()
        scope = posixpath.normpath(raw_scope) if raw_scope else ""
        if not scope:
            return True
        if not raw_requested:
            return True
        requested = posixpath.normpath(raw_requested)
        # A direct module retry inherits its own constraint, descendants inherit
        # it, and a parent reactor must intersect every constrained module it
        # contains.  A true sibling/disjoint project remains isolated.
        return (
            requested == scope
            or requested.startswith(scope.rstrip("/") + "/")
            or scope.startswith(requested.rstrip("/") + "/")
        )

    def _empty_overlay(self) -> dict[str, Any]:
        return {"version": 1, "tools": {}}

    def _tool_entry(self, overlay: dict[str, Any], tool: str) -> dict[str, Any]:
        return overlay.setdefault("tools", {}).setdefault(
            tool,
            {"candidates": {}, "blocked": []},
        )

    def _is_blocked_in_overlay(
        self,
        overlay: dict[str, Any],
        tool: str,
        executable: str,
        *,
        version: Optional[str] = None,
        requirement: Optional[str] = None,
    ) -> bool:
        for block in overlay.get("tools", {}).get(tool, {}).get("blocked", []):
            if block.get("executable") != executable:
                continue
            if version is not None and block.get("version") not in (None, str(version)):
                continue
            if requirement is not None and block.get("requirement") not in (None, requirement):
                continue
            return True
        return False

    def _activate_in_overlay(
        self,
        overlay: dict[str, Any],
        tool: str,
        executable: str,
    ) -> None:
        entry = overlay.get("tools", {}).get(tool)
        if not entry or executable not in entry.get("candidates", {}):
            raise ValueError(f"{executable} is not registered for {tool}")
        if self._is_blocked_in_overlay(overlay, tool, executable):
            raise ValueError(f"{executable} is blocked for {tool}")
        entry["active"] = executable

    def _normalize_tool(self, tool: str) -> str:
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError("tool is required")
        normalized = tool.strip().lower()
        return _TOOL_ALIASES.get(normalized, normalized)

    def _normalize_executable(self, executable: str) -> str:
        if not isinstance(executable, str) or not executable.strip():
            raise ValueError("executable is required")
        return executable.strip()

    def _normalize_tool_executable(self, tool: str, executable: str) -> str:
        normalized = self._normalize_executable(executable)
        if self._normalize_tool(tool) == "maven" and not posixpath.isabs(normalized):
            raise ValueError("Maven executable must be an absolute container path")
        return normalized

    def _normalize_env(self, env: dict[str, Any]) -> dict[str, str]:
        if not isinstance(env, dict):
            raise ValueError("env must be an object")
        normalized: dict[str, str] = {}
        for key, value in env.items():
            if not isinstance(key, str) or not _ENV_NAME_RE.match(key):
                raise ValueError(f"Invalid env key: {key!r}")
            normalized[key] = str(value)
        return normalized

    def _normalize_path_prepend(
        self,
        path_prepend: Optional[list[str] | str],
        executable: str,
    ) -> list[str]:
        executable_directory = posixpath.dirname(executable)
        normalized: list[str] = []
        if executable_directory and executable_directory != ".":
            normalized.append(executable_directory)

        if path_prepend is None:
            return normalized
        if isinstance(path_prepend, str):
            entries = [path_prepend]
        elif isinstance(path_prepend, list):
            entries = path_prepend
        else:
            raise ValueError("path_prepend must be a string or list of strings")

        for entry in entries:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError("path_prepend entries must be non-empty strings")
            cleaned = entry.strip()
            if cleaned not in normalized:
                normalized.append(cleaned)
        return normalized

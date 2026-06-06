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
        executable_path = self._normalize_executable(executable)
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
        executable_path = self._normalize_executable(executable)
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
        executable_path = self._normalize_executable(executable)
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

    def clear(self, tool: Optional[str] = None) -> dict[str, Any]:
        """Clear the whole overlay or one tool entry."""
        overlay, _warnings = self._load_overlay()
        if tool is None:
            overlay = self._empty_overlay()
        else:
            overlay.setdefault("tools", {}).pop(self._normalize_tool(tool), None)
        return self._write_overlay(overlay)

    def active_candidate(self, tool: str) -> dict[str, Any] | None:
        """Return the active candidate metadata for a tool, including its executable."""
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
            self._normalize_executable(executable),
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
        self._write_file(self.overlay_json, payload)
        self._write_file(self.overlay_script, script)
        return deepcopy(normalized)

    def _render_shell_script(self, overlay: dict[str, Any]) -> str:
        lines = ["# Generated by Setup-Agent env overlay."]
        path_entries: list[str] = []
        seen_paths: set[str] = set()

        for tool_name in sorted(overlay.get("tools", {})):
            tool_entry = overlay["tools"][tool_name]
            active = tool_entry.get("active")
            if not active:
                continue
            candidate = tool_entry.get("candidates", {}).get(active)
            if not candidate:
                continue

            for key, value in sorted(candidate.get("env", {}).items()):
                lines.append(f"export {key}={shlex.quote(str(value))}")

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

        result = self.orchestrator.execute_command(f"cat {shlex.quote(path)} 2>/dev/null || true")
        if result.get("exit_code", 0) != 0:
            return None
        return result.get("output", "")

    def _write_file(self, path: str, content: str) -> None:
        if hasattr(self.orchestrator, "write_file"):
            result = self.orchestrator.write_file(path, content)
            if isinstance(result, dict) and result.get("success") is False:
                raise RuntimeError(f"Failed to write {path}: {result.get('output', '')}")
            return

        payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
        command = f"printf %s {shlex.quote(payload)} | base64 -d > {shlex.quote(path)}"
        result = self.orchestrator.execute_command(command)
        if result.get("exit_code", 0) != 0:
            raise RuntimeError(f"Failed to write {path}: {result.get('output', '')}")

    def _ensure_overlay_dir(self) -> None:
        directory = posixpath.dirname(self.overlay_json)
        result = self.orchestrator.execute_command(f"mkdir -p {shlex.quote(directory)}")
        if result.get("exit_code", 0) != 0:
            raise RuntimeError(f"Failed to create {directory}: {result.get('output', '')}")

    def _normalize_overlay(
        self,
        overlay: dict[str, Any],
        *,
        tolerant: bool = False,
        warnings: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        normalized = self._empty_overlay()
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
                        executable = self._normalize_executable(raw_executable)
                        raw_env = (
                            candidate.get("env") if candidate.get("env") is not None else {}
                        )
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
                                "executable": self._normalize_executable(
                                    raw_block["executable"]
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

            normalized["tools"][tool_name] = entry

        return normalized

    def _append_warning(self, warnings: Optional[list[str]], message: str) -> None:
        if warnings is not None:
            warnings.append(message)

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
        return tool.strip().lower()

    def _normalize_executable(self, executable: str) -> str:
        if not isinstance(executable, str) or not executable.strip():
            raise ValueError("executable is required")
        return executable.strip()

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
        if path_prepend is None:
            directory = posixpath.dirname(executable)
            return [directory] if directory and directory != "." else []
        if isinstance(path_prepend, str):
            entries = [path_prepend]
        elif isinstance(path_prepend, list):
            entries = path_prepend
        else:
            raise ValueError("path_prepend must be a string or list of strings")

        normalized: list[str] = []
        for entry in entries:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError("path_prepend entries must be non-empty strings")
            if entry not in normalized:
                normalized.append(entry)
        return normalized

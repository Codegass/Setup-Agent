"""Agent-facing tool for runtime environment overlays."""

from __future__ import annotations

import json
from typing import Any, Optional

from sag.runtime.env_overlay import EnvOverlayStore

from .base import BaseTool, ToolResult


class EnvTool(BaseTool):
    """Manage agent-maintained runtime environment overlay entries."""

    def __init__(self, orchestrator: Any, store: Optional[EnvOverlayStore] = None):
        super().__init__(
            name="env",
            description=(
                "Manage runtime env overlay entries for tool executable paths, PATH prefixes, "
                "and environment variables. Use bash to download or install runtimes, then use "
                "env register after installation. Use env activate before retrying a build. Use "
                "env block for exact executable/version negative evidence from build errors. Do "
                "not use env to edit project build files, and do not use env to install or "
                "download software."
            ),
        )
        self.store = store or EnvOverlayStore(orchestrator)

    def execute(
        self,
        action: str | dict[str, Any],
        tool: Optional[str] = None,
        executable: Optional[str] = None,
        version: Optional[str] = None,
        source: Optional[str] = None,
        env: Optional[dict[str, Any]] = None,
        path_prepend: Optional[list[str] | str] = None,
        activate: bool = False,
        requirement: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> ToolResult:
        """Execute an env overlay action."""
        params = self._normalize_request(
            action=action,
            tool=tool,
            executable=executable,
            version=version,
            source=source,
            env=env,
            path_prepend=path_prepend,
            activate=activate,
            requirement=requirement,
            reason=reason,
        )

        try:
            action_name = params["action"]
            if action_name == "inspect":
                overlay = self.store.inspect()
                return self._result("inspect", overlay)

            if action_name == "register":
                overlay = self.store.register(
                    params["tool"],
                    params["executable"],
                    version=params.get("version"),
                    source=params.get("source", "agent_registered"),
                    env=params.get("env"),
                    path_prepend=params.get("path_prepend"),
                    activate=bool(params.get("activate", False)),
                )
                return self._result("register", overlay)

            if action_name == "activate":
                overlay = self.store.activate(params["tool"], params["executable"])
                return self._result(
                    "activate",
                    overlay,
                    active_candidate=self.store.active_candidate(params["tool"]),
                )

            if action_name == "block":
                overlay = self.store.block(
                    params["tool"],
                    params["executable"],
                    version=params.get("version"),
                    requirement=params.get("requirement"),
                    reason=params.get("reason"),
                    source=params.get("source", "build_error"),
                )
                return self._result("block", overlay)

            if action_name == "clear":
                overlay = self.store.clear(params.get("tool"))
                return self._result("clear", overlay)

            return ToolResult(
                success=False,
                output="",
                error=f"Invalid env action: {action_name}",
                error_code="ENV_INVALID_ACTION",
                suggestions=[
                    "Use one of: inspect, register, activate, block, clear.",
                ],
                raw_data={"action": action_name},
            )
        except KeyError as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Missing required env parameter: {exc.args[0]}",
                error_code="ENV_MISSING_PARAMETER",
                suggestions=[
                    "Provide tool and executable for register, activate, and block actions."
                ],
                raw_data={"action": params.get("action"), "missing": exc.args[0]},
            )
        except ValueError as exc:
            return ToolResult(
                success=False,
                output="",
                error=str(exc),
                error_code="ENV_VALIDATION_ERROR",
                suggestions=[
                    "Inspect the overlay and register a valid candidate before activating it."
                ],
                raw_data={"action": params.get("action")},
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Env overlay operation failed: {exc}",
                error_code="ENV_OPERATION_FAILED",
                suggestions=[
                    "Check that the Docker workspace is writable and try the env action again."
                ],
                raw_data={"action": params.get("action")},
            )

    def _normalize_request(self, **kwargs: Any) -> dict[str, Any]:
        action = kwargs.pop("action")
        if isinstance(action, dict):
            params = {key: value for key, value in action.items() if value is not None}
        else:
            params = {"action": action}
            params.update({key: value for key, value in kwargs.items() if value is not None})

        if "action" not in params or not str(params["action"]).strip():
            raise ValueError("action is required")
        params["action"] = str(params["action"]).strip().lower()
        return params

    def _result(
        self,
        action: str,
        overlay: dict[str, Any],
        *,
        active_candidate: Optional[dict[str, Any]] = None,
    ) -> ToolResult:
        raw_data: dict[str, Any] = {"action": action, "overlay": overlay}
        if active_candidate is not None:
            raw_data["active_candidate"] = active_candidate
        return ToolResult(
            success=True,
            output=json.dumps(raw_data, indent=2, sort_keys=True),
            raw_data=raw_data,
            metadata={"action": action},
        )

    def _get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["inspect", "register", "activate", "block", "clear"],
                    "description": "Env overlay action to perform.",
                },
                "tool": {
                    "type": "string",
                    "description": "Tool name, such as maven.",
                },
                "executable": {
                    "type": "string",
                    "description": "Exact executable path to register, activate, or block.",
                },
                "version": {
                    "type": "string",
                    "description": "Observed executable version.",
                },
                "source": {
                    "type": "string",
                    "description": (
                        "Evidence source for the overlay entry. Defaults to "
                        "agent_registered for register and build_error for block."
                    ),
                },
                "env": {
                    "type": "object",
                    "description": "Environment variables to export when this candidate is active.",
                },
                "path_prepend": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "PATH entries to prepend when this candidate is active.",
                },
                "activate": {
                    "type": "boolean",
                    "description": "Activate the candidate during register.",
                    "default": False,
                },
                "requirement": {
                    "type": "string",
                    "description": "Requirement that the blocked executable failed to satisfy.",
                },
                "reason": {
                    "type": "string",
                    "description": "Human-readable reason for a block entry.",
                },
            },
            "required": ["action"],
        }

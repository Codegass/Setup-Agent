"""Private tool recovery strategies for orchestrated tool execution."""

from __future__ import annotations

from typing import Any, Dict, Optional

from loguru import logger as default_logger

from sag.agent.tool_orchestration import RecoveryDecision
from sag.tools.base import BaseTool, ToolResult


class ToolRecoveryHandler:
    """Recover selected tool failures without exposing a public module API."""

    def __init__(
        self,
        *,
        tools: Dict[str, BaseTool],
        context_manager: Any,
        repository_url: Optional[str],
        logger: Any = None,
    ) -> None:
        self.tools = tools
        self.context_manager = context_manager
        self.repository_url = repository_url
        self.logger = logger or default_logger

    def recover(
        self, tool_name: str, params: Dict[str, Any], failed_result: ToolResult
    ) -> RecoveryDecision:
        """Return a recovery decision for a failed tool result."""
        try:
            error_msg = failed_result.error or "Unknown error"
            self.logger.info(
                f"Attempting tool recovery for {tool_name}: {error_msg[:100]}"
            )

            if tool_name == "manage_context":
                return self._recover_context_management_error(params, failed_result)
            if tool_name == "project_setup":
                return self._recover_project_setup_error(params, failed_result)
            return self._recover_generic_error(tool_name, params, failed_result)
        except Exception as exc:
            message = f"Recovery mechanism failed: {exc}"
            self.logger.error(f"Tool recovery itself failed for {tool_name}: {exc}")
            return self._no_strategy("generic_no_strategy", message)

    def _recover_context_management_error(
        self, params: Dict[str, Any], failed_result: ToolResult
    ) -> RecoveryDecision:
        action = params.get("action", "")
        error_code = failed_result.error_code

        if error_code == "NO_ACTIVE_TASK" and action in {
            "complete_task",
            "complete_with_results",
        }:
            try:
                trunk_context = self.context_manager.load_trunk_context()
                if trunk_context:
                    in_progress_tasks = [
                        task
                        for task in trunk_context.todo_list
                        if task.status.value == "in_progress"
                    ]

                    if len(in_progress_tasks) == 1:
                        recovered_task = in_progress_tasks[0]
                        message = (
                            f"Recovered by setting current task to {recovered_task.id}"
                        )
                    elif len(in_progress_tasks) > 1:
                        recovered_task = max(in_progress_tasks, key=lambda task: task.id)
                        message = (
                            "Recovered by choosing most recent task "
                            f"{recovered_task.id} from multiple in-progress"
                        )
                    else:
                        recovered_task = None
                        message = ""

                    if recovered_task:
                        self.context_manager.current_task_id = recovered_task.id
                        result = self.tools["manage_context"].safe_execute(**params)
                        return self._attempted(
                            strategy="manage_context_active_task",
                            message=message,
                            result=result,
                            recovery_params=params,
                        )
            except Exception as exc:
                self.logger.warning(f"Context recovery failed: {exc}")

        return self._no_strategy(None, "No recovery strategy applicable")

    def _recover_project_setup_error(
        self, params: Dict[str, Any], failed_result: ToolResult
    ) -> RecoveryDecision:
        action = params.get("action", "")

        if action == "clone" and not params.get("repository_url") and self.repository_url:
            recovery_params = dict(params)
            recovery_params["repository_url"] = self.repository_url
            result = self.tools["project_setup"].safe_execute(**recovery_params)
            return self._attempted(
                strategy="project_setup_repository_url",
                message=f"Recovered by injecting repository URL: {self.repository_url}",
                result=result,
                recovery_params=recovery_params,
            )

        return self._no_strategy(
            None, "No project setup recovery strategy applicable"
        )

    def _recover_generic_error(
        self, tool_name: str, params: Dict[str, Any], failed_result: ToolResult
    ) -> RecoveryDecision:
        return self._no_strategy(
            "generic_no_strategy", "No generic recovery strategy available"
        )

    def _attempted(
        self,
        *,
        strategy: str,
        message: str,
        result: ToolResult,
        recovery_params: Dict[str, Any],
    ) -> RecoveryDecision:
        metadata = {
            "attempted": True,
            "success": result.success,
            "message": message,
            "strategy": strategy,
            "replacement_result_success": result.success,
            "recovery_params": recovery_params,
        }
        return RecoveryDecision(
            should_recover=True,
            strategy=strategy,
            guidance=message,
            replacement_result=result,
            replacement_params=recovery_params,
            metadata=metadata,
        )

    def _no_strategy(
        self, strategy: Optional[str], message: str
    ) -> RecoveryDecision:
        return RecoveryDecision(
            should_recover=False,
            strategy=strategy,
            guidance=message,
            metadata={
                "attempted": False,
                "success": False,
                "message": message,
                "strategy": strategy,
            },
        )

"""Private tool recovery strategies for orchestrated tool execution."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from loguru import logger as default_logger

from sag.agent.tool_orchestration import RecoveryDecision
from sag.tools.base import ActualToolExecution, BaseTool, OutputPersistenceError, ToolResult
from sag.tools.build.backends import GradleBackend, MavenBackend


class ToolRecoveryHandler:
    """Recover selected tool failures without exposing a public module API."""

    def __init__(
        self,
        *,
        tools: Dict[str, BaseTool],
        context_manager: Any,
        successful_states: Dict[str, Any],
        repository_url: Optional[str],
        repository_ref: Optional[str] = None,
        add_system_guidance: Any,
        logger: Any = None,
    ) -> None:
        self.tools = tools
        self.context_manager = context_manager
        self.successful_states = successful_states
        self.repository_url = repository_url
        self.repository_ref = repository_ref
        self.add_system_guidance = add_system_guidance
        self.logger = logger or default_logger

    def recover(
        self, tool_name: str, params: Dict[str, Any], failed_result: ToolResult
    ) -> RecoveryDecision:
        """Return a recovery decision for a failed tool result."""
        try:
            error_msg = failed_result.error or "Unknown error"
            self.logger.info(f"Attempting tool recovery for {tool_name}: {error_msg[:100]}")

            if tool_name == "manage_context":
                decision = self._recover_context_management_error(params, failed_result)
            elif tool_name == "project_setup":
                decision = self._recover_project_setup_error(params, failed_result)
            elif tool_name == "project":
                # Stage-1 surface: only the clone verb has a recovery strategy.
                if str(params.get("action", "")).lower() == "clone":
                    decision = self._recover_project_setup_error(
                        self._project_clone_params(params), failed_result
                    )
                else:
                    decision = self._recover_generic_error(tool_name, params, failed_result)
            elif tool_name == "maven":
                decision = self._recover_maven_error(params, failed_result)
            elif tool_name == "gradle":
                decision = self._recover_gradle_error(params, failed_result)
            elif tool_name == "build":
                decision = self._recover_build_error(params, failed_result)
            elif tool_name == "bash":
                decision = self._recover_bash_error(params, failed_result)
            elif tool_name == "file_io":
                decision = self._recover_file_io_error(params, failed_result)
            else:
                decision = self._recover_generic_error(tool_name, params, failed_result)
        except OutputPersistenceError:
            raise
        except Exception as exc:
            message = f"Recovery mechanism failed: {exc}"
            self.logger.error(f"Tool recovery itself failed for {tool_name}: {exc}")
            return self._no_strategy("generic_no_strategy", message)

        if decision.replacement_result is not None and decision.replacement_tool_name is None:
            decision.replacement_tool_name = tool_name
        return decision

    def _delegate_tool(self, name: str) -> Optional[BaseTool]:
        """Resolve a backend/delegate tool by its legacy name.

        Direct registrations win when present (tests, transitional setups);
        otherwise the stage-1 facades' internals are used — the registry no
        longer carries 'maven'/'gradle'/'project_setup'/'system'.
        """
        tool = self.tools.get(name)
        if tool is not None:
            return tool
        if name in ("maven", "gradle"):
            build = self.tools.get("build")
            backend = getattr(build, "_backends", {}).get(name) if build is not None else None
            return getattr(backend, f"{name}_tool", None)
        project = self.tools.get("project")
        if project is None:
            return None
        attribute = {
            "project_setup": "setup_tool",
            "project_analyzer": "analyzer_tool",
            "system": "system_tool",
            "env": "env_tool",
        }.get(name)
        return getattr(project, attribute, None) if attribute else None

    def _project_clone_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """project(action='clone', repo_url=...) -> ProjectSetupTool vocabulary."""
        translated = dict(params)
        repo_url = translated.pop("repo_url", None)
        if repo_url and not translated.get("repository_url"):
            translated["repository_url"] = repo_url
        translated.setdefault("action", "clone")
        return translated

    def _recover_build_error(
        self, params: Dict[str, Any], failed_result: ToolResult
    ) -> RecoveryDecision:
        """Route consolidated build failures to the backend-specific strategies."""
        system = (getattr(failed_result, "facts", None) or {}).get("system")
        if system == "maven":
            decision = self._recover_maven_error(
                self._maven_params_from_build(params), failed_result
            )
            decision.replacement_tool_name = "maven"
            return decision
        if system == "gradle":
            decision = self._recover_gradle_error(
                self._gradle_params_from_build(params), failed_result
            )
            decision.replacement_tool_name = "gradle"
            return decision
        return self._no_strategy("build_no_strategy", "No build recovery strategy applicable")

    def _maven_params_from_build(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """build(action=...) -> MavenTool vocabulary (action -> command)."""
        action = str(params.get("action", "")).strip().lower()
        translated: Dict[str, Any] = {
            "command": MavenBackend.VERBS.get(action, action or "compile")
        }
        if params.get("args"):
            translated["extra_args"] = params["args"]
        if params.get("working_directory"):
            translated["working_directory"] = params["working_directory"]
        if params.get("timeout"):
            translated["timeout"] = params["timeout"]
        return translated

    @staticmethod
    def _build_action_for_maven_command(command: str) -> str:
        """Map a Maven command back onto a valid build(action=...) for guidance."""
        normalized = str(command or "").strip().lower()
        reverse = {maven_cmd: verb for verb, maven_cmd in MavenBackend.VERBS.items()}
        if normalized in reverse:
            return reverse[normalized]
        if normalized in ("deps", "compile", "test", "package"):
            return normalized
        return "compile"

    def _gradle_params_from_build(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """build(action=...) -> GradleTool vocabulary (action -> tasks)."""
        action = str(params.get("action", "")).strip().lower()
        translated: Dict[str, Any] = {
            "tasks": GradleBackend.VERBS.get(action, action or "compileJava")
        }
        if params.get("args"):
            translated["gradle_args"] = params["args"]
        if params.get("working_directory"):
            translated["working_directory"] = params["working_directory"]
        if params.get("timeout"):
            translated["timeout"] = params["timeout"]
        return translated

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
                        message = f"Recovered by setting current task to {recovered_task.id}"
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

        if error_code == "INVALID_TASK_ID" and action == "start_task":
            try:
                trunk_context = self.context_manager.load_trunk_context()
                if trunk_context:
                    next_task = trunk_context.get_next_pending_task()
                    if next_task:
                        recovery_params = dict(params)
                        recovery_params["task_id"] = next_task.id
                        result = self.tools["manage_context"].safe_execute(**recovery_params)
                        return self._attempted(
                            strategy="manage_context_invalid_task_id",
                            message=f"Recovered by using next valid task ID: {next_task.id}",
                            result=result,
                            recovery_params=recovery_params,
                        )
            except Exception as exc:
                self.logger.warning(f"Task ID recovery failed: {exc}")

        return self._no_strategy(None, "No recovery strategy applicable")

    def _recover_project_setup_error(
        self, params: Dict[str, Any], failed_result: ToolResult
    ) -> RecoveryDecision:
        action = params.get("action", "")

        setup_tool = self._delegate_tool("project_setup")
        if (
            action == "clone"
            and not params.get("repository_url")
            and self.repository_url
            and setup_tool is not None
        ):
            recovery_params = dict(params)
            recovery_params["repository_url"] = self.repository_url
            if self.repository_ref and not recovery_params.get("ref"):
                recovery_params["ref"] = self.repository_ref
            result = setup_tool.safe_execute(**recovery_params)
            return self._attempted(
                strategy="project_setup_repository_url",
                message=(
                    f"Recovered by injecting repository URL: {self.repository_url}"
                    + (f" and ref: {self.repository_ref}" if self.repository_ref else "")
                ),
                result=result,
                recovery_params=recovery_params,
            )

        return self._no_strategy(None, "No project setup recovery strategy applicable")

    def _recover_maven_error(
        self, params: Dict[str, Any], failed_result: ToolResult
    ) -> RecoveryDecision:
        """Recover from Maven tool failures."""
        error_msg = failed_result.error or ""
        error_code = failed_result.error_code or ""
        metadata = failed_result.metadata or {}
        analysis = metadata.get("analysis", {})

        if error_code.startswith("TIMEOUT_"):
            return self._maven_timeout_guidance(params, failed_result)

        maven_version_requirement = metadata.get("maven_version_requirement") or analysis.get(
            "maven_version_requirement"
        )
        if maven_version_requirement:
            return self._maven_version_contract_guidance(
                params, failed_result, maven_version_requirement
            )

        if error_code == "JAVA_VERSION_MISMATCH":
            decision = self._recover_maven_java_version(params, failed_result)
            if decision.should_recover:
                return decision

        if self._is_maven_missing_project(error_code, error_msg, analysis):
            decision = self._recover_maven_pom_discovery(params)
            if decision.should_recover:
                return decision

        maven_tool = self._delegate_tool("maven")
        if maven_tool is None:
            return self._no_strategy("maven_no_strategy", "No Maven recovery strategy applicable")

        if self._is_maven_working_directory_error(error_code, error_msg, analysis):
            if self.successful_states.get("working_directory"):
                recovery_params = dict(params)
                recovery_params["working_directory"] = self.successful_states["working_directory"]

                result = maven_tool.safe_execute(**recovery_params)
                return self._attempted(
                    strategy="maven_known_working_directory",
                    message=(
                        "Recovered by using known working directory: "
                        f"{self.successful_states['working_directory']}"
                    ),
                    result=result,
                    recovery_params=recovery_params,
                )

        command = params.get("command", "")
        if "test" in command and "compilation" in error_msg.lower():
            recovery_params = dict(params)
            recovery_params["command"] = "compile"

            result = maven_tool.safe_execute(**recovery_params)
            return self._attempted(
                strategy="maven_compile_before_test",
                message="Recovered by trying compile before test",
                result=result,
                recovery_params=recovery_params,
            )

        if analysis:
            decision = self._recover_maven_exclusions(params, analysis)
            if decision.should_recover:
                return decision

        return self._no_strategy("maven_no_strategy", "No Maven recovery strategy applicable")

    def _maven_version_contract_guidance(
        self,
        params: Dict[str, Any],
        failed_result: ToolResult,
        requirement: Dict[str, Any],
    ) -> RecoveryDecision:
        raw_requirement = requirement.get("raw", "the project-required range")
        runtime = (failed_result.metadata or {}).get("maven_runtime", {})
        executable = runtime.get("executable", "the current Maven executable")
        version = runtime.get("version", "unknown")
        command = self._build_action_for_maven_command(params.get("command", "compile"))
        guidance = (
            "MAVEN VERSION REQUIREMENT: The current Maven runtime does not satisfy "
            f"{raw_requirement}. Current executable: {executable}; version: {version}. "
            "Do not retry the same Maven executable. Use bash to download or unpack a "
            "compatible Maven distribution, then register its bin/mvn via "
            "project(action='env', tool='maven', executable=...), "
            f"and retry build(action='{command}')."
        )

        self.add_system_guidance(guidance, priority="high")

        return self._guidance_only(
            strategy="maven_version_contract_guidance",
            message=guidance,
            result=failed_result,
            params=params,
        )

    def _recover_maven_java_version(
        self, params: Dict[str, Any], failed_result: ToolResult
    ) -> RecoveryDecision:
        metadata = failed_result.metadata
        analysis = metadata.get("analysis", {})
        java_error = analysis.get("java_version_error", {})
        required_version = java_error.get("required")
        current_version = java_error.get("current", "unknown")

        if not required_version:
            return self._no_strategy("maven_no_strategy", "No Maven recovery strategy applicable")

        self.logger.info(
            "Attempting Java version recovery: installing Java "
            f"{required_version} (current: {current_version})"
        )

        system_tool = self._delegate_tool("system")
        maven_tool = self._delegate_tool("maven")
        if system_tool is None or maven_tool is None:
            self.logger.warning("System tool not available for Java installation")
            return self._no_strategy("maven_no_strategy", "No Maven recovery strategy applicable")

        verify_params = {"action": "verify_java", "java_version": required_version}
        verify_result = system_tool.safe_execute(**verify_params)
        actual_executions = [ActualToolExecution("system", verify_params, verify_result)]
        if verify_result.succeeded:
            result = maven_tool.safe_execute(**params)
            actual_executions.append(ActualToolExecution("maven", dict(params), result))
            return self._attempted(
                strategy="maven_java_version",
                message=(
                    f"Java {required_version} was already installed, " "retried Maven command"
                ),
                result=result.with_execution_trace(actual_executions),
                recovery_params=params,
            )

        install_params = {
            "action": "install_java",
            "java_version": required_version,
        }
        install_result = system_tool.safe_execute(**install_params)
        actual_executions.append(ActualToolExecution("system", install_params, install_result))

        if install_result.succeeded:
            result = maven_tool.safe_execute(**params)
            actual_executions.append(ActualToolExecution("maven", dict(params), result))
            return self._attempted(
                strategy="maven_java_version",
                message=(f"Recovered by installing Java {required_version} and retrying"),
                result=result.with_execution_trace(actual_executions),
                recovery_params=params,
            )

        return self._attempted(
            strategy="maven_java_version",
            message=f"Attempted to install Java {required_version} but failed",
            result=install_result.with_execution_trace(actual_executions),
            recovery_params=params,
            metadata={"repair_params": install_params},
        )

    def _recover_maven_pom_discovery(self, params: Dict[str, Any]) -> RecoveryDecision:
        orchestrator = getattr(self.context_manager, "orchestrator", None)
        if not orchestrator:
            return self._no_strategy("maven_no_strategy", "No Maven recovery strategy applicable")

        try:
            locate_cmd = "find /workspace -maxdepth 4 -name pom.xml | head -20"
            locate_res = orchestrator.execute_command(locate_cmd)
            pom_candidates = (locate_res.get("output") or "").strip().splitlines()
            project_name = getattr(orchestrator, "project_name", None)
            target_pom = None

            if project_name:
                root_candidate = f"/workspace/{project_name}/pom.xml"
                if root_candidate in pom_candidates:
                    target_pom = root_candidate

            if not target_pom and project_name:
                scoped = [
                    candidate
                    for candidate in pom_candidates
                    if candidate.startswith(f"/workspace/{project_name}/")
                ]
                if scoped:
                    scoped.sort(key=lambda path: path.count("/"))
                    target_pom = scoped[0]

            if not target_pom and pom_candidates:
                target_pom = pom_candidates[0]

            maven_tool = self._delegate_tool("maven")
            if target_pom and maven_tool is not None:
                recovery_params = dict(params)
                recovery_params["pom_file"] = target_pom
                recovery_params["working_directory"] = os.path.dirname(target_pom)
                result = maven_tool.safe_execute(**recovery_params)
                return self._attempted(
                    strategy="maven_pom_discovery",
                    message=f"Recovered by targeting detected pom: {target_pom}",
                    result=result,
                    recovery_params=recovery_params,
                )
        except Exception as exc:
            self.logger.warning(f"Automatic pom.xml discovery failed during Maven recovery: {exc}")

        return self._no_strategy("maven_no_strategy", "No Maven recovery strategy applicable")

    def _recover_maven_exclusions(
        self, params: Dict[str, Any], analysis: Dict[str, Any]
    ) -> RecoveryDecision:
        failed_modules: List[str] = []
        for module in analysis.get("failed_modules", []):
            artifact = module.get("artifact_id")
            if artifact:
                failed_modules.append(artifact)
            else:
                pom_path = module.get("pom_path")
                if pom_path:
                    failed_modules.append(Path(pom_path).parent.name)

        failed_tests = [
            self._format_test_exclusion(test) for test in analysis.get("failed_tests", [])
        ]

        recovery_params = dict(params)
        recovery_params["fail_at_end"] = True
        new_exclusions = False

        if failed_modules:
            excluded_modules: Set[str] = self.successful_states.setdefault(
                "excluded_modules", set()
            )
            for module_name in failed_modules:
                if module_name and module_name not in excluded_modules:
                    excluded_modules.add(module_name)
                    new_exclusions = True
            if excluded_modules and new_exclusions:
                props = self._normalize_properties(recovery_params.get("properties"))
                props = [prop for prop in props if not prop.startswith("-pl")]
                module_clause = ",".join(f"!{name}" for name in sorted(excluded_modules))
                props.append(f"-pl {module_clause}")
                props = self._ensure_flag(props, "-am")
                recovery_params["properties"] = props

        if failed_tests:
            excluded_tests: Set[str] = self.successful_states.setdefault("excluded_tests", set())
            added_test = False
            for test_name in failed_tests:
                if test_name and test_name not in excluded_tests:
                    excluded_tests.add(test_name)
                    added_test = True
            if excluded_tests and added_test:
                props = self._normalize_properties(recovery_params.get("properties"))
                test_clause = "!" + ",!".join(sorted(excluded_tests))
                props = self._set_property(props, "test=", test_clause)
                recovery_params["properties"] = props
                new_exclusions = True

        maven_tool = self._delegate_tool("maven")
        if new_exclusions and maven_tool is not None:
            result = maven_tool.safe_execute(**recovery_params)
            return self._attempted(
                strategy="maven_exclude_modules_or_tests",
                message=("Recovered by excluding failing modules/tests and rerunning Maven"),
                result=result,
                recovery_params=recovery_params,
            )

        return self._no_strategy("maven_no_strategy", "No Maven recovery strategy applicable")

    def _maven_timeout_guidance(
        self, params: Dict[str, Any], failed_result: ToolResult
    ) -> RecoveryDecision:
        metadata = failed_result.metadata
        termination_reason = metadata.get("termination_reason", "unknown")
        execution_time = metadata.get("execution_time", 0)
        command = params.get("command", "unknown")
        guidance = [
            f"Maven command '{command}' timed out after {execution_time:.1f}s",
            "Consider breaking down the Maven build into smaller phases",
            "Try running 'mvn dependency:resolve' first to download dependencies",
            "Consider using '-T 1C' flag for parallel builds",
            "Check if the build is waiting for user input or network resources",
            "For large projects, consider building specific modules with '-pl' flag",
        ]

        self.add_system_guidance(
            f"MAVEN TIMEOUT: The Maven command '{command}' timed out after "
            f"{execution_time:.1f}s. This is often due to dependency downloads "
            "or large compilation tasks. Consider breaking the build into phases.",
            priority="high",
        )

        result = ToolResult.completed_failure(
            output=(
                f"Maven command timed out after {execution_time:.1f}s due to "
                f"{termination_reason}.\n\nSuggestions:\n"
                + "\n".join(f"- {item}" for item in guidance)
            ),
            error=f"Maven command timed out ({termination_reason})",
            error_code="MAVEN_TIMEOUT_HANDLED",
            suggestions=guidance,
            metadata={
                "timeout_handled": True,
                "execution_time": execution_time,
                "termination_reason": termination_reason,
                "original_command": command,
                "tool_type": "maven",
            },
        )
        return self._guidance_only(
            strategy="maven_timeout_guidance",
            message=(
                "Maven timeout handled gracefully - provided guidance for " "alternative approaches"
            ),
            result=result,
            params=params,
        )

    def _recover_gradle_error(
        self, params: Dict[str, Any], failed_result: ToolResult
    ) -> RecoveryDecision:
        """Recover from Gradle tool failures."""
        error_msg = failed_result.error or ""
        error_code = failed_result.error_code or ""

        if error_code.startswith("TIMEOUT_"):
            return self._gradle_timeout_guidance(params, failed_result)

        gradle_tool = self._delegate_tool("gradle")
        if gradle_tool is None:
            return self._no_strategy("gradle_no_strategy", "No Gradle recovery strategy applicable")

        if self._is_gradle_working_directory_error(error_code, error_msg):
            if self.successful_states.get("working_directory"):
                recovery_params = dict(params)
                recovery_params["working_directory"] = self.successful_states["working_directory"]
                result = gradle_tool.safe_execute(**recovery_params)
                return self._attempted(
                    strategy="gradle_known_working_directory",
                    message=(
                        "Recovered by using known working directory: "
                        f"{self.successful_states['working_directory']}"
                    ),
                    result=result,
                    recovery_params=recovery_params,
                )

        task = self._gradle_task_value(params)
        if "test" in task and "compilation" in error_msg.lower():
            recovery_params = dict(params)
            recovery_params.pop("task", None)
            recovery_params.pop("command", None)
            recovery_params["tasks"] = "compileJava"
            result = gradle_tool.safe_execute(**recovery_params)
            return self._attempted(
                strategy="gradle_compile_before_test",
                message="Recovered by trying compileJava before test",
                result=result,
                recovery_params=recovery_params,
            )

        return self._no_strategy("gradle_no_strategy", "No Gradle recovery strategy applicable")

    def _recover_bash_error(
        self, params: Dict[str, Any], failed_result: ToolResult
    ) -> RecoveryDecision:
        """Recover from bash tool failures."""
        error_msg = failed_result.error or ""
        error_code = failed_result.error_code or ""

        if error_code.startswith("TIMEOUT_"):
            return self._bash_timeout_guidance(params, failed_result)

        metadata = failed_result.metadata or {}
        exit_code = metadata.get("exit_code", 0)
        if metadata and (
            exit_code == 127
            or "OCI runtime exec failed" in error_msg
            or "no such file or directory" in error_msg
        ):
            decision = self._recover_bash_workspace_recreation(params)
            if decision.should_recover:
                return decision

        if self.successful_states.get("working_directory"):
            recovery_params = dict(params)
            recovery_params["working_directory"] = self.successful_states["working_directory"]
            result = self.tools["bash"].safe_execute(**recovery_params)
            return self._attempted(
                strategy="bash_known_working_directory",
                message=(
                    "Recovered by using known working directory: "
                    f"{self.successful_states['working_directory']}"
                ),
                result=result,
                recovery_params=recovery_params,
            )

        return self._no_strategy("bash_no_strategy", "No bash recovery strategy applicable")

    def _bash_timeout_guidance(
        self, params: Dict[str, Any], failed_result: ToolResult
    ) -> RecoveryDecision:
        metadata = failed_result.metadata or {}
        termination_reason = metadata.get("termination_reason", "")
        execution_time = metadata.get("monitoring_info", {}).get("execution_time", 0)
        command = params.get("command", "")

        if "mvn" in command or "maven" in command:
            timeout_guidance = [
                "Maven command timed out - this is common for large projects",
                "Consider breaking down into smaller steps: build(action='compile'), then build(action='test')",
                "Use the build tool instead of bash for better timeout handling",
                "For multi-module projects, build(action='test') continues past module failures (fail-at-end automatic)",
            ]
        elif "gradle" in command:
            timeout_guidance = [
                "Gradle command timed out - use the build tool for better timeout handling",
                "Consider breaking down into smaller tasks",
                "Try with --parallel flag for faster execution",
            ]
        else:
            timeout_guidance = [
                f"Command '{command}' exceeded timeout limits",
                "Consider breaking the task into smaller steps",
                "Check if the command requires user interaction",
                "Investigate if the process is stuck waiting for resources",
            ]

        self.add_system_guidance(
            f"TIMEOUT HANDLED: The command '{command}' timed out after "
            f"{execution_time:.1f}s. This is a normal timeout, not a system "
            "failure. Consider alternative approaches or breaking the task into "
            "smaller steps.",
            priority="high",
        )

        result = ToolResult.completed_failure(
            output=(
                f"Command timed out after {execution_time:.1f}s due to "
                f"{termination_reason}.\n\nSuggestions:\n"
                + "\n".join(f"- {item}" for item in timeout_guidance)
            ),
            error=f"Command timed out ({termination_reason})",
            error_code="TIMEOUT_HANDLED",
            suggestions=timeout_guidance,
            metadata={
                "timeout_handled": True,
                "execution_time": execution_time,
                "termination_reason": termination_reason,
                "original_command": command,
            },
        )
        return self._guidance_only(
            strategy="bash_timeout_guidance",
            message=(
                "Timeout handled gracefully - provided guidance for alternative " "approaches"
            ),
            result=result,
            params=params,
        )

    def _recover_bash_workspace_recreation(self, params: Dict[str, Any]) -> RecoveryDecision:
        recovery_steps = [
            ("mkdir -p /workspace", "Create workspace directory"),
            ("chmod 755 /workspace", "Set workspace permissions"),
            ("touch /workspace/.sag_workspace_marker", "Create workspace marker"),
        ]

        workspace_fixed = True
        step_results = []
        for recovery_cmd, description in recovery_steps:
            recovery_result = self._execute_workspace_recovery_command(recovery_cmd)
            step_results.append(
                {
                    "command": recovery_cmd,
                    "description": description,
                    "success": bool(recovery_result.get("success")),
                }
            )
            if not recovery_result.get("success"):
                workspace_fixed = False
                break

        if not workspace_fixed:
            message = "Failed to recreate workspace directory"
            return RecoveryDecision(
                should_recover=True,
                strategy="bash_workspace_recreation",
                guidance=message,
                metadata={
                    "attempted": True,
                    "success": False,
                    "message": message,
                    "strategy": "bash_workspace_recreation",
                    "workspace_recovery_steps": step_results,
                },
            )

        recovery_params = dict(params)
        recovery_params["working_directory"] = "/workspace"
        result = self.tools["bash"].safe_execute(**recovery_params)
        message = (
            "Recovered by recreating workspace directory and retrying command"
            if result.succeeded
            else "Workspace recreated but command still failed - may be a different issue"
        )
        return self._attempted(
            strategy="bash_workspace_recreation",
            message=message,
            result=result,
            recovery_params=recovery_params,
            metadata={"workspace_recovery_steps": step_results},
        )

    def _execute_workspace_recovery_command(self, command: str) -> Dict[str, Any]:
        orchestrator = getattr(self.context_manager, "orchestrator", None)
        if orchestrator:
            try:
                return orchestrator.execute_command(command, workdir=None)
            except TypeError:
                return orchestrator.execute_command(command)

        bash_tool = self.tools.get("bash")
        if bash_tool:
            bash_result = bash_tool.safe_execute(command=command, working_directory="/")
            return {
                "success": bash_result.succeeded,
                "output": bash_result.output,
            }

        return {
            "success": False,
            "output": "No recovery mechanism available",
        }

    def _recover_file_io_error(
        self, params: Dict[str, Any], failed_result: ToolResult
    ) -> RecoveryDecision:
        """Recover from file I/O tool failures."""
        action = params.get("action", "")
        path = params.get("path", "")

        if action == "read" and "not found" in (failed_result.error or "").lower():
            if self.successful_states.get("working_directory") and not path.startswith("/"):
                recovery_params = dict(params)
                recovery_params["path"] = f"{self.successful_states['working_directory']}/{path}"
                result = self.tools["file_io"].safe_execute(**recovery_params)
                return self._attempted(
                    strategy="file_io_known_working_directory",
                    message="Recovered by adjusting path with working directory",
                    result=result,
                    recovery_params=recovery_params,
                )

        return self._no_strategy("file_io_no_strategy", "No file I/O recovery strategy applicable")

    def _gradle_timeout_guidance(
        self, params: Dict[str, Any], failed_result: ToolResult
    ) -> RecoveryDecision:
        metadata = failed_result.metadata
        termination_reason = metadata.get("termination_reason", "unknown")
        execution_time = metadata.get("execution_time", 0)
        task = self._gradle_task_value(params) or "unknown"
        guidance = [
            f"Gradle task '{task}' timed out after {execution_time:.1f}s",
            "Consider breaking down the Gradle build into smaller tasks",
            "Try running './gradlew dependencies' first to download dependencies",
            "Consider using '--parallel' flag for parallel builds",
            "Check if the build is waiting for user input or network resources",
            "For large projects, consider building specific modules or subprojects",
            "Use '--info' or '--debug' flags to monitor build progress",
        ]

        self.add_system_guidance(
            f"GRADLE TIMEOUT: The Gradle task '{task}' timed out after "
            f"{execution_time:.1f}s. This is often due to dependency downloads "
            "or large compilation tasks. Consider breaking the build into phases.",
            priority="high",
        )

        result = ToolResult.completed_failure(
            output=(
                f"Gradle task timed out after {execution_time:.1f}s due to "
                f"{termination_reason}.\n\nSuggestions:\n"
                + "\n".join(f"- {item}" for item in guidance)
            ),
            error=f"Gradle task timed out ({termination_reason})",
            error_code="GRADLE_TIMEOUT_HANDLED",
            suggestions=guidance,
            metadata={
                "timeout_handled": True,
                "execution_time": execution_time,
                "termination_reason": termination_reason,
                "original_task": task,
                "tool_type": "gradle",
            },
        )
        return self._guidance_only(
            strategy="gradle_timeout_guidance",
            message=(
                "Gradle timeout handled gracefully - provided guidance for "
                "alternative approaches"
            ),
            result=result,
            params=params,
        )

    def _recover_generic_error(
        self, tool_name: str, params: Dict[str, Any], failed_result: ToolResult
    ) -> RecoveryDecision:
        return self._no_strategy("generic_no_strategy", "No generic recovery strategy available")

    def _is_maven_missing_project(
        self, error_code: str, error_msg: str, analysis: Dict[str, Any]
    ) -> bool:
        error_lower = error_msg.lower()
        return (
            error_code == "NO_POM_XML"
            or analysis.get("error_type") == "MISSING_PROJECT"
            or "no pom.xml found" in error_lower
            or ("pom" in error_lower and "not" in error_lower)
        )

    def _is_maven_working_directory_error(
        self, error_code: str, error_msg: str, analysis: Dict[str, Any]
    ) -> bool:
        error_lower = error_msg.lower()
        return (
            self._is_maven_missing_project(error_code, error_msg, analysis)
            or "not found" in error_lower
            or "no such file" in error_lower
        )

    def _is_gradle_working_directory_error(self, error_code: str, error_msg: str) -> bool:
        error_lower = error_msg.lower()
        return (
            error_code == "BUILD_FILE_NOT_FOUND"
            or "no build.gradle" in error_lower
            or ("build.gradle" in error_lower and "not" in error_lower)
            or "not found" in error_lower
            or "no such file" in error_lower
        )

    def _gradle_task_value(self, params: Dict[str, Any]) -> str:
        return str(params.get("tasks") or params.get("command") or params.get("task") or "")

    def _attempted(
        self,
        *,
        strategy: str,
        message: str,
        result: ToolResult,
        recovery_params: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RecoveryDecision:
        recovery_metadata = {
            "attempted": True,
            "success": result.succeeded,
            "message": message,
            "strategy": strategy,
            "replacement_result_succeeded": result.succeeded,
            "recovery_params": recovery_params,
        }
        if metadata:
            recovery_metadata.update(metadata)

        return RecoveryDecision(
            should_recover=True,
            strategy=strategy,
            guidance=message,
            replacement_result=result,
            replacement_params=recovery_params,
            metadata=recovery_metadata,
        )

    def _no_strategy(self, strategy: Optional[str], message: str) -> RecoveryDecision:
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

    def _guidance_only(
        self,
        *,
        strategy: str,
        message: str,
        result: ToolResult,
        params: Dict[str, Any],
    ) -> RecoveryDecision:
        metadata = {
            "attempted": True,
            "success": False,
            "message": message,
            "strategy": strategy,
            "replacement_result_succeeded": result.succeeded,
            "recovery_params": params,
            "guidance_only": True,
        }
        return RecoveryDecision(
            should_recover=True,
            strategy=strategy,
            guidance=message,
            replacement_result=result,
            replacement_params=params,
            metadata=metadata,
        )

    def _normalize_properties(self, raw_props: Any) -> List[str]:
        if not raw_props:
            return []
        if isinstance(raw_props, list):
            return [prop for prop in raw_props if prop]
        if isinstance(raw_props, str):
            return [prop.strip() for prop in raw_props.split(",") if prop.strip()]
        return [str(raw_props)]

    def _ensure_flag(self, props: List[str], flag: str) -> List[str]:
        if flag not in props:
            props.append(flag)
        return props

    def _set_property(self, props: List[str], prefix: str, value: str) -> List[str]:
        updated = [prop for prop in props if not prop.startswith(prefix)]
        updated.append(f"{prefix}{value}")
        return updated

    def _format_test_exclusion(self, name: str) -> str:
        cleaned = (name or "").strip()
        if not cleaned:
            return cleaned
        if "." in cleaned and "#" not in cleaned:
            cls, method = cleaned.rsplit(".", 1)
            return f"{cls}#{method}"
        return cleaned

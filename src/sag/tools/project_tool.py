"""project(action: clone | provision | analyze | env) — setup-time facade.

Delegates to the existing ProjectSetupTool / ProjectAnalyzerTool /
SystemTool / EnvTool (stage 1: surface consolidation only)."""

from typing import Any, Dict

from .base import BaseTool, ToolResult


class ProjectTool(BaseTool):
    def __init__(self, setup_tool=None, analyzer_tool=None, system_tool=None, env_tool=None):
        from sag.config.prescriptions import prescription_flags

        # Treatment mask dim (a): THIS description is what the prompt builder
        # actually injects (panel review: masking only the inner analyzer
        # tool's description left 'plan' in the initial prompt — the facade
        # is the registered surface).
        analyze_wording = (
            "analyze (detect build system, plan)"
            if prescription_flags()["plan_pipeline"]
            else "analyze (survey the project; persist build facts)"
        )
        super().__init__(
            name="project",
            description=(
                "Project lifecycle: action = clone (repo_url[, ref]) | "
                "provision (install toolchain: java_version for a JDK, packages for apt) | "
                f"{analyze_wording} | "
                "env (register env vars/executables; tool + executable [+ env])."
            ),
        )
        self.setup_tool = setup_tool
        self.analyzer_tool = analyzer_tool
        self.system_tool = system_tool
        self.env_tool = env_tool
        # BaseTool auto-derives the validation schema from execute(action,
        # **kwargs), which hides every delegated parameter and makes
        # safe_execute reject repo_url/java_version/... as UNEXPECTED_PARAMETERS.
        # Validate against the documented facade schema instead.
        self._parameter_schema = self._get_parameters_schema()

    def execute(self, action: str, **kwargs) -> ToolResult:
        verb = (action or "").strip().lower()
        routes = {
            "clone": self.setup_tool,
            "provision": self.system_tool,
            "analyze": self.analyzer_tool,
            "env": self.env_tool,
        }
        if verb not in routes:
            return ToolResult.completed_failure(
                output=f"Unknown project action: {action!r}",
                error="invalid action",
                suggestions=["Use action= clone | provision | analyze | env"],
            )
        delegate = routes[verb]
        if delegate is None:
            return ToolResult.completed_failure(
                output=f"{verb} unavailable",
                error="delegate missing",
            )
        if verb == "clone":
            # ProjectSetupTool's real parameter is repository_url; accept the
            # facade's repo_url spelling and translate.
            if "repo_url" in kwargs:
                kwargs.setdefault("repository_url", kwargs.pop("repo_url"))
            kwargs.setdefault("action", "clone")
            return delegate.execute(**kwargs)
        if verb == "provision":
            # SystemTool's verbs are its own action vocabulary:
            # install_java for JDKs, install for apt packages.
            if "packages" in kwargs and "java_version" not in kwargs:
                kwargs.setdefault("action", "install")
            else:
                kwargs.setdefault("action", "install_java")
            return delegate.execute(**kwargs)
        if verb == "analyze":
            kwargs.setdefault("action", "analyze")
            return delegate.execute(**kwargs)
        # env: EnvTool's vocabulary is inspect|register|activate|block|clear;
        # register is its "set env vars/executables" verb (there is no "set").
        kwargs.setdefault("action", "register")
        return delegate.execute(**kwargs)

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["clone", "provision", "analyze", "env"],
                },
                "repo_url": {"type": "string", "description": "clone: repository URL"},
                "ref": {"type": "string", "description": "clone: git ref (optional)"},
                "project_path": {"type": "string", "description": "analyze: project directory"},
                "java_version": {"type": "string", "description": "provision: JDK version"},
                "packages": {"type": "array", "description": "provision: apt packages to install"},
                "tool": {
                    "type": "string",
                    "description": "env: tool name to register (e.g. 'java')",
                },
                "executable": {"type": "string", "description": "env: executable path to register"},
                "env": {"type": "object", "description": "env: variables to set"},
            },
            "required": ["action"],
            # The delegates accept more than the documented surface
            # (target_directory, update_context, version, activate,
            # path_prepend, ...); pass everything through to them.
            "additionalProperties": True,
        }

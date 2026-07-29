"""project(action: clone | provision | analyze | env) — setup-time facade.

Delegates to the existing ProjectSetupTool / ProjectAnalyzerTool /
SystemTool / EnvTool (stage 1: surface consolidation only)."""

from typing import Any, Dict

from .base import BaseTool, ToolResult


class ProjectTool(BaseTool):
    def __init__(self, setup_tool=None, analyzer_tool=None, system_tool=None, env_tool=None):
        super().__init__(
            name="project",
            description=(
                "Project lifecycle: action = clone (repo_url[, ref]) | "
                "provision (install toolchain: java_version for a JDK, packages for apt) | "
                "analyze (survey the project; persist build facts) | "
                "env (validate, register, and activate a runtime executable; "
                "tool + executable [+ env])."
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
        unexpected = self._unaccepted_parameters(delegate, kwargs)
        if unexpected:
            # Live p7b-camel-quarkus: the model passed JAVA_HOME=... to
            # project(action='env') and the run recorded "Tool project crashed:
            # EnvTool.execute() got an unexpected keyword argument". A facade
            # that forwards **kwargs into a typed signature turns every wrong
            # guess into a crash; a wrong parameter is a thing to refuse and
            # name, never a thing to fall over on.
            accepted = self._accepted_parameters(delegate)
            suggestions = [
                f"project(action='{verb}') accepts: "
                + ", ".join(sorted(accepted - {"self", "action"}))
            ]
            if verb == "env" and any(name.isupper() for name in unexpected):
                suggestions.insert(
                    0,
                    "Environment variables go in env={...}, e.g. "
                    "project(action='env', tool='java', executable='/path/to/java', "
                    "env={'JAVA_HOME': '/path/to/jdk'})",
                )
            return ToolResult.completed_failure(
                output="",
                error=f"unexpected parameter(s) for project(action='{verb}'): "
                + ", ".join(sorted(unexpected)),
                error_code="PROJECT_UNEXPECTED_PARAMETERS",
                suggestions=suggestions,
                raw_data={
                    "action": verb,
                    "unexpected": sorted(unexpected),
                    "accepted": sorted(accepted - {"self", "action"}),
                },
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
        # The public facade is the one-step recovery surface taught to the
        # model, so a proven runtime must become active atomically.  Direct
        # EnvTool callers retain its conservative activate=False default; the
        # public facade deliberately has no inactive-registration escape hatch.
        if kwargs.get("activate") is False:
            return ToolResult.completed_failure(
                output="",
                error="project(action='env') requires atomic runtime activation",
                error_code="PROJECT_ENV_ACTIVATION_REQUIRED",
                suggestions=[
                    "Remove activate=false; a successful project env call always activates "
                    "the validated executable."
                ],
                raw_data={"action": "env", "activation_required": True},
            )
        kwargs.setdefault("action", "register")
        kwargs["activate"] = True
        return delegate.execute(**kwargs)

    @staticmethod
    def _accepted_parameters(delegate: Any) -> set:
        """Parameter names the delegate's own signature accepts.

        A delegate that takes **kwargs accepts anything, so nothing is
        refused for it — the facade must not be stricter than the tool it
        forwards to.
        """
        import inspect

        try:
            signature = inspect.signature(delegate.execute)
        except (TypeError, ValueError):
            return set()
        names = set()
        for parameter in signature.parameters.values():
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                return set()  # accepts anything
            if parameter.name != "self":
                names.add(parameter.name)
        return names

    def _unaccepted_parameters(self, delegate: Any, kwargs: Dict[str, Any]) -> set:
        """Supplied names the delegate cannot take, after the facade's own
        translations (`repo_url` -> `repository_url`) are accounted for."""
        accepted = self._accepted_parameters(delegate)
        if not accepted:
            return set()
        translated = {"repo_url"} if "repository_url" in accepted else set()
        return {name for name in kwargs if name not in accepted and name not in translated}

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
                    "description": "env: tool name to register and activate (e.g. 'maven')",
                },
                "executable": {
                    "type": "string",
                    "description": (
                        "env: absolute container executable path to validate, canonicalize, "
                        "register, and activate"
                    ),
                },
                "env": {"type": "object", "description": "env: variables to set"},
                "activate": {
                    "type": "boolean",
                    "enum": [True],
                    "default": True,
                    "description": (
                        "env: must be true; a successful public env call atomically "
                        "activates the validated executable"
                    ),
                },
                "requirement": {
                    "type": "string",
                    "description": (
                        "env: version requirement the measured executable must satisfy "
                        "(for example '[3.9,)' for Maven)"
                    ),
                },
            },
            "required": ["action"],
            # The delegates accept more than the documented surface
            # (target_directory, update_context, version, activate,
            # path_prepend, ...); pass everything through to them.
            "additionalProperties": True,
        }

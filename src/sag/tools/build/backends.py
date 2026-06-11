"""Build backends: ecosystem-specific verb mappings (spec §4 growth law).

Each backend maps the invariant verbs (deps/compile/test/package) onto an
underlying tool. Stage 1 delegates to the existing MavenTool/GradleTool;
later ecosystems (python/node) add a module here, never a schema change.
"""

from typing import Any, Dict, Optional

from sag.tools.base import ToolResult

# Marker files probed (in priority order) to select a backend.
BUILD_MARKERS = {
    "maven": ("pom.xml",),
    "gradle": ("build.gradle", "build.gradle.kts", "settings.gradle",
               "settings.gradle.kts", "gradlew"),
}


class MavenBackend:
    VERBS = {
        "deps": "dependency:resolve",
        "compile": "compile",
        "test": "test",
        "package": "package",
    }

    def __init__(self, maven_tool):
        self.maven_tool = maven_tool

    def run(self, verb: str, args: Optional[str], working_directory: str,
            timeout: Optional[int]) -> ToolResult:
        kwargs: Dict[str, Any] = {
            "command": self.VERBS[verb],
            "working_directory": working_directory,
        }
        if verb == "test":
            kwargs["fail_at_end"] = True
        if args:
            kwargs["extra_args"] = args
        if timeout:
            kwargs["timeout"] = timeout
        return self.maven_tool.execute(**kwargs)


class GradleBackend:
    VERBS = {
        "deps": "dependencies",
        "compile": "compileJava",
        "test": "test",
        "package": "assemble",
    }

    def __init__(self, gradle_tool):
        self.gradle_tool = gradle_tool

    def run(self, verb: str, args: Optional[str], working_directory: str,
            timeout: Optional[int]) -> ToolResult:
        kwargs: Dict[str, Any] = {
            "tasks": self.VERBS[verb],
            "working_directory": working_directory,
        }
        if verb == "test":
            kwargs["fail_at_end"] = True
        if args:
            kwargs["gradle_args"] = args
        if timeout:
            kwargs["timeout"] = timeout
        return self.gradle_tool.execute(**kwargs)
